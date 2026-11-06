import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from guard_classifier_outage import (
    BackfillStatus,
    COOLDOWN_SECONDS,
    ClassifierOutageRecovery,
    NotificationPhase,
    OutageState,
)
from test_gate import FakeProvider, GateEvent, gate_config, plugin


VALID_PROBE_RESPONSE = (
    '{"act":"silent","conf":10,"understood":true,'
    '"reason":"no_value","ground":"none"}'
)


class NotificationContext:
    def __init__(self, provider, admins=None):
        self.provider = provider
        self.config = {"admins_id": list(admins or [])}
        self.sent = []

    def get_provider_by_id(self, _provider_id):
        return self.provider

    async def get_using_provider_async(self, _origin=None):
        return self.provider

    def get_config(self):
        return self.config

    async def send_message(self, route, chain):
        text = "".join(str(getattr(part, "text", "")) for part in chain.chain)
        self.sent.append((route, text))
        return True


class ResolverFailureContext(NotificationContext):
    def get_provider_by_id(self, _provider_id):
        raise RuntimeError("private code failure")


class HttpError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__("upstream body and sk-secret must stay private")


class CapturingProvider(FakeProvider):
    def __init__(self, text):
        super().__init__(text)
        self.requests = []

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        return await super().text_chat(**kwargs)


class SequenceProvider:
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = 0
        self.requests = []

    async def text_chat(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(completion_text=result)


class BlockingProvider:
    def __init__(self):
        self.calls = 0
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def text_chat(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(completion_text=VALID_PROBE_RESPONSE)


class ProviderMapContext(NotificationContext):
    def __init__(self, providers, admins=None):
        super().__init__(None, admins)
        self.providers = providers

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)


class CrossPluginContext(NotificationContext):
    def __init__(self, provider, congling, admins=None):
        super().__init__(provider, admins)
        self.congling = congling

    def get_registered_star(self, name):
        if name not in {"astrbot_plugin_congling", "congling"}:
            return None
        if self.congling is None:
            return None
        return SimpleNamespace(activated=True, star_cls=self.congling)


async def wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


class ClassifierOutageFlowTests(unittest.TestCase):
    @staticmethod
    def request():
        return SimpleNamespace(extra_user_content_parts=[], contexts=[])

    def ready(self, provider, *, admins=None, **overrides):
        config = gate_config(
            classifier_outage_recovery_enabled=True,
            classifier_provider_id="router::public-model",
            **overrides,
        )
        instance = plugin(config, provider)
        instance.context = NotificationContext(provider, admins)
        return instance

    @staticmethod
    def event(text="这条消息需要判定", group="1001", explicit=False):
        event = GateEvent(text=text, group=group)
        event.get_platform_id = lambda: "qq-bot"
        event.is_at_or_wake_command = explicit
        return event

    def test_recoverable_failure_enters_cooling_notifies_and_redacts_log(self):
        provider = FakeProvider(HttpError(503))
        guard = self.ready(provider, admins=["10001", "10001", "10002"])
        event = self.event("正文 secret prompt response sk-secret")

        asyncio.run(guard.guard_llm_request(event, self.request()))

        self.assertTrue(event.stopped)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(guard._outage().state, OutageState.COOLING)
        outage = guard._outage().current_event
        self.assertEqual(outage.error_category, "http_5xx")
        self.assertEqual(set(outage.affected_sessions), {event.unified_msg_origin})
        self.assertEqual(
            [route for route, _ in guard.context.sent],
            ["qq-bot:FriendMessage:10001", "qq-bot:FriendMessage:10002"],
        )
        payload = "\n".join(text for _, text in guard.context.sent)
        for secret in ("正文 secret", "prompt", "response", "sk-secret"):
            self.assertNotIn(secret, payload)
        row = guard.engine.log.recent(1)[0]
        self.assertEqual(row["failure_category"], "http_5xx")
        self.assertEqual(row["raw"], "")
        self.assertEqual(row["ctx"], "")
        self.assertTrue(row["chat_data"]["outage_redacted"])
        self.assertNotIn("text", row["chat_data"]["current_message"])

    def test_cooling_enforce_skips_api_and_tracks_each_session(self):
        provider = FakeProvider(HttpError(429))
        guard = self.ready(provider)
        first = self.event(group="1001")
        asyncio.run(guard.guard_llm_request(first, self.request()))

        second = self.event(group="1002")
        asyncio.run(guard.guard_llm_request(second, self.request()))

        self.assertTrue(second.stopped)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            set(guard._outage().current_event.affected_sessions),
            {first.unified_msg_origin, second.unified_msg_origin},
        )
        row = guard.engine.log.recent(1)[0]
        self.assertEqual(row["src"], "outage")
        self.assertNotIn("raw", row)
        self.assertNotIn("ctx", row)
        self.assertNotIn("chat_data", row)

    def test_cooling_shadow_skips_api_and_never_queues_backfill(self):
        provider = FakeProvider(ConnectionResetError("private upstream response"))
        guard = self.ready(provider, gate_mode="shadow")
        first = self.event(group="1001")
        asyncio.run(guard.guard_llm_request(first, self.request()))
        second = self.event(group="1002")

        asyncio.run(guard.guard_llm_request(second, self.request()))

        self.assertFalse(first.stopped)
        self.assertFalse(second.stopped)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(guard._outage().current_event.affected_sessions, {})
        self.assertIn(
            "classifier_cooling",
            second.get_extra("_at_guard_observation_rules"),
        )

    def test_explicit_call_bypasses_cooling(self):
        provider = FakeProvider(HttpError(503))
        guard = self.ready(provider)
        asyncio.run(
            guard.guard_llm_request(self.event(group="1001"), self.request())
        )
        explicit = self.event(group="1002", explicit=True)

        asyncio.run(guard.guard_llm_request(explicit, self.request()))

        self.assertFalse(explicit.stopped)
        self.assertEqual(provider.calls, 1)
        self.assertNotIn(
            explicit.unified_msg_origin,
            guard._outage().current_event.affected_sessions,
        )

    def test_authentication_and_missing_provider_need_attention(self):
        for provider, category in (
            (FakeProvider(HttpError(401)), "authentication_error"),
            (FakeProvider(ValueError("private config value")), "configuration_error"),
            (None, "provider_missing"),
        ):
            with self.subTest(category=category):
                guard = self.ready(provider)
                event = self.event()
                asyncio.run(guard.guard_llm_request(event, self.request()))
                self.assertEqual(guard._outage().state, OutageState.NEEDS_ATTENTION)
                self.assertEqual(
                    guard._outage().current_event.error_category, category
                )

    def test_empty_timeout_and_protocol_failures_are_recoverable(self):
        for provider, category in (
            (FakeProvider(""), "empty_response"),
            (FakeProvider("not valid classifier json"), "protocol_error"),
            (FakeProvider(TimeoutError("private request")), "timeout"),
        ):
            with self.subTest(category=category):
                guard = self.ready(provider)
                event = self.event()
                asyncio.run(guard.guard_llm_request(event, self.request()))
                self.assertEqual(guard._outage().state, OutageState.COOLING)
                self.assertEqual(
                    guard._outage().current_event.error_category, category
                )

    def test_unknown_code_error_fails_closed_without_creating_outage(self):
        provider = FakeProvider("unused")
        guard = self.ready(provider)
        guard.context = ResolverFailureContext(provider)
        event = self.event()

        asyncio.run(guard.guard_llm_request(event, self.request()))

        self.assertTrue(event.stopped)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(
            event.get_extra("_at_guard_decision")["reason"], "error"
        )
        self.assertIsNone(guard._outage().current_event)

    def test_budget_and_semantic_silence_do_not_create_outage(self):
        provider = FakeProvider(
            '{"act":"silent","conf":10,"understood":true,'
            '"reason":"others_talking","ground":"none"}'
        )
        guard = self.ready(provider, gate_hourly_budget=1)
        guard.engine._budget["1001"] = deque([time.monotonic()])
        budget_event = self.event(group="1001")
        asyncio.run(guard.guard_llm_request(budget_event, self.request()))
        self.assertEqual(
            budget_event.get_extra("_at_guard_decision")["reason"], "budget"
        )
        self.assertIsNone(guard._outage().current_event)

        semantic_event = self.event(group="1002")
        asyncio.run(guard.guard_llm_request(semantic_event, self.request()))
        self.assertEqual(
            semantic_event.get_extra("_at_guard_decision")["reason"],
            "others_talking",
        )
        self.assertIsNone(guard._outage().current_event)

    def test_notification_throttle_and_recovery_interface(self):
        provider = FakeProvider(HttpError(503))
        guard = self.ready(provider, admins=["10001"])
        event = self.event()
        asyncio.run(guard.guard_llm_request(event, self.request()))
        outage = guard._outage().current_event
        initial = outage.notifications[0].sent_at

        sent = asyncio.run(guard.notify_classifier_outage_continued(
            platform_id="qq-bot", cycle=1, at=initial + 599
        ))
        self.assertEqual(sent, 0)
        sent = asyncio.run(guard.notify_classifier_outage_continued(
            platform_id="qq-bot", cycle=1, at=initial + 600
        ))
        self.assertEqual(sent, 1)
        sent = asyncio.run(guard.notify_classifier_recovered(
            platform_id="qq-bot",
            counts={"completed": 2, "no_reply": 1, "retry": 0},
            at=initial + 1200,
        ))
        self.assertEqual(sent, 1)
        self.assertIn("文字判定状态：恢复", guard.context.sent[-1][1])
        self.assertIn("成功 2，无需回复 1，待重试 0", guard.context.sent[-1][1])

    def test_no_admin_keeps_state_without_public_notification(self):
        provider = FakeProvider(HttpError(503))
        guard = self.ready(provider, admins=[])
        event = self.event()

        asyncio.run(guard.guard_llm_request(event, self.request()))

        self.assertEqual(guard._outage().state, OutageState.COOLING)
        self.assertEqual(guard.context.sent, [])

    def test_runtime_enable_syncs_before_first_failure(self):
        provider = FakeProvider(HttpError(503))
        guard = plugin(gate_config(), provider)
        guard.context = NotificationContext(provider)
        self.assertEqual(guard._outage().state, OutageState.DISABLED)
        guard.config["classifier_outage_recovery_enabled"] = True
        event = self.event()

        asyncio.run(guard.guard_llm_request(event, self.request()))

        self.assertEqual(guard._outage().state, OutageState.COOLING)


class ClassifierProbeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, provider, *, admins=None, **overrides):
        config = gate_config(
            classifier_outage_recovery_enabled=True,
            classifier_provider_id="provider-a",
            **overrides,
        )
        guard = plugin(config, provider)
        guard.context = NotificationContext(provider, admins)
        return guard

    async def test_expired_cooling_probes_synthetic_data_without_stats_or_budget(self):
        provider = CapturingProvider(VALID_PROBE_RESPONSE)
        guard = self.ready(provider)
        now = time.time()
        event = guard._outage().create_event(
            "timeout", "provider-a", at=now - COOLDOWN_SECONDS - 1
        )
        guard._outage().add_affected_session(
            "qq:group:private-scope", at=now - 300
        )
        before_stats = dict(guard.engine.stats)
        before_budget = dict(guard.engine.budget_usage())
        before_participation = dict(guard.participation_stats)
        before_log = list(guard.engine.log.ring)
        callbacks = []
        guard.register_classifier_recovery_callback(callbacks.append)

        self.assertTrue(guard.request_classifier_outage_probe(reason="test_due"))
        await wait_until(
            lambda: guard._outage().state is OutageState.RECOVERING
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(guard.engine.stats, before_stats)
        self.assertEqual(guard.engine.budget_usage(), before_budget)
        self.assertEqual(guard.participation_stats, before_participation)
        self.assertEqual(list(guard.engine.log.ring), before_log)
        self.assertEqual(guard.context.sent, [])
        request = provider.requests[0]
        payload = json.dumps(request, ensure_ascii=False, default=str)
        self.assertEqual(request["session_id"], "at_guard_classifier_health_probe")
        self.assertEqual(request["contexts"], [])
        self.assertNotIn("private-scope", payload)
        work = guard.pending_classifier_recovery()
        self.assertEqual(work.event_id, event.event_id)
        self.assertEqual(work.window_end, event.recovery_window_end)
        self.assertEqual(callbacks, [work])
        await guard.terminate()

    async def test_recoverable_probe_failure_recools_and_notifies_once(self):
        provider = CapturingProvider(HttpError(503))
        guard = self.ready(provider, admins=["10001"])
        now = time.time()
        outage = guard._outage()
        outage.create_event(
            "timeout", "provider-a", at=now - COOLDOWN_SECONDS - 1
        )
        outage.mark_notification(
            NotificationPhase.INTERRUPTED,
            "10001",
            at=now - COOLDOWN_SECONDS - 1,
        )

        guard.request_classifier_outage_probe(
            reason="test_due", platform_id="qq-bot"
        )
        await wait_until(
            lambda: provider.calls == 1
            and outage.state is OutageState.COOLING
            and len(guard.context.sent) == 1
        )

        self.assertGreaterEqual(
            outage.current_event.cooldown_until, time.time() + 590
        )
        self.assertIn("持续不可用", guard.context.sent[0][1])
        self.assertEqual(
            [
                receipt.phase
                for receipt in outage.current_event.notifications
            ],
            [NotificationPhase.INTERRUPTED, NotificationPhase.CONTINUED],
        )
        timer = guard._classifier_probe_task
        self.assertIsNotNone(timer)
        await guard.terminate()
        self.assertTrue(timer.cancelled())
        self.assertEqual(outage.state, OutageState.COOLING)

    async def test_first_failure_owns_timer_and_auto_probes_at_expiry(self):
        provider = SequenceProvider(HttpError(503), VALID_PROBE_RESPONSE)
        guard = self.ready(provider)
        event = GateEvent(text="真实用户正文不可进入探测", group="1001")
        event.get_platform_id = lambda: "qq-bot"
        request = SimpleNamespace(extra_user_content_parts=[], contexts=[])

        with patch("guard_classifier_outage.COOLDOWN_SECONDS", 0.02):
            await guard.guard_llm_request(event, request)
            await wait_until(
                lambda: guard._outage().state is OutageState.RECOVERING
            )

        self.assertEqual(provider.calls, 2)
        probe_payload = json.dumps(
            provider.requests[1], ensure_ascii=False, default=str
        )
        self.assertNotIn("真实用户正文", probe_payload)
        self.assertTrue(event.stopped)
        await guard.terminate()

    async def test_probe_rejects_malformed_protocol_and_recools(self):
        provider = CapturingProvider("not classifier json")
        guard = self.ready(provider)
        outage = guard._outage()
        outage.create_event(
            "timeout",
            "provider-a",
            at=time.time() - COOLDOWN_SECONDS - 1,
        )

        guard.request_classifier_outage_probe(reason="test_due")
        await wait_until(
            lambda: provider.calls == 1
            and outage.state is OutageState.COOLING
            and outage.current_event.error_category == "protocol_error"
        )

        self.assertGreater(
            outage.current_event.cooldown_until, time.time() + 590
        )
        await guard.terminate()

    async def test_needs_attention_recovers_after_configuration_fix(self):
        provider = CapturingProvider(VALID_PROBE_RESPONSE)
        guard = self.ready(provider)
        guard._outage().create_event(
            "authentication_error",
            "provider-old",
            state=OutageState.NEEDS_ATTENTION,
            at=time.time() - 10,
        )

        guard.request_classifier_outage_probe(
            immediate=True, reason="config_saved"
        )
        await wait_until(
            lambda: guard._outage().state is OutageState.RECOVERING
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            guard.pending_classifier_recovery().provider_id, "provider-a"
        )
        await guard.terminate()

    async def test_reload_immediately_reclaims_persisted_cooling_event(self):
        with TemporaryDirectory() as directory:
            provider = CapturingProvider(VALID_PROBE_RESPONSE)
            config = gate_config(
                classifier_outage_recovery_enabled=True,
                classifier_provider_id="provider-a",
            )
            old = ClassifierOutageRecovery(
                config, Path(directory), SimpleNamespace(warning=lambda *_: None)
            )
            event = old.create_event(
                "timeout", "provider-a", at=time.time()
            )
            old.add_affected_session("qq:group:1001", at=time.time())
            old.mark_notification(
                NotificationPhase.INTERRUPTED,
                "10001",
                at=time.time(),
            )
            guard = plugin(config, provider)
            guard.classifier_outage = ClassifierOutageRecovery(
                config, Path(directory), guard.logger
            )
            guard.context = NotificationContext(provider, admins=["10001"])

            with patch("guard_webui.register_webui", lambda *_: None):
                await guard.initialize()
            await wait_until(
                lambda: guard._outage().state is OutageState.RECOVERING
            )

            self.assertEqual(provider.calls, 1)
            self.assertEqual(
                guard.pending_classifier_recovery().event_id, event.event_id
            )
            self.assertEqual(guard.context.sent, [])
            self.assertEqual(
                len(guard._outage().current_event.notifications), 1
            )
            await guard.terminate()

    async def test_terminate_cancels_future_timer_without_state_write(self):
        provider = CapturingProvider(VALID_PROBE_RESPONSE)
        guard = self.ready(provider)
        guard._outage().create_event(
            "timeout", "provider-a", at=time.time()
        )
        guard.request_classifier_outage_probe(reason="cooldown_started")
        timer = guard._classifier_probe_task
        await asyncio.sleep(0)

        await guard.terminate()

        self.assertTrue(timer.cancelled())
        self.assertEqual(provider.calls, 0)
        self.assertEqual(guard._outage().state, OutageState.COOLING)
        self.assertFalse(
            guard.request_classifier_outage_probe(
                immediate=True, reason="after_terminate"
            )
        )

    async def test_provider_switch_during_probe_discards_old_result_without_overlap(self):
        old_provider = BlockingProvider()
        new_provider = CapturingProvider(VALID_PROBE_RESPONSE)
        guard = self.ready(old_provider)
        guard.context = ProviderMapContext({
            "provider-a": old_provider,
            "provider-b": new_provider,
        })
        guard._outage().create_event(
            "timeout", "provider-a", at=time.time() - COOLDOWN_SECONDS
        )
        guard.request_classifier_outage_probe(
            immediate=True, reason="first_config"
        )
        task = guard._classifier_probe_task
        await old_provider.started.wait()
        blocked = GateEvent(text="探测期间的普通消息", group="1002")
        await guard.guard_llm_request(
            blocked,
            SimpleNamespace(extra_user_content_parts=[], contexts=[]),
        )
        self.assertTrue(blocked.stopped)
        self.assertEqual(old_provider.calls, 1)

        guard.config["classifier_provider_id"] = "provider-b"
        for _ in range(5):
            self.assertTrue(guard.request_classifier_outage_probe(
                immediate=True, reason="provider_switched"
            ))
            self.assertIs(guard._classifier_probe_task, task)
        old_provider.release.set()
        await wait_until(
            lambda: guard._outage().state is OutageState.RECOVERING
        )

        self.assertEqual(old_provider.calls, 1)
        self.assertEqual(new_provider.calls, 1)
        self.assertEqual(
            guard.pending_classifier_recovery().provider_id, "provider-b"
        )
        await guard.terminate()


class ClassifierRecoveryHandoffTests(unittest.IsolatedAsyncioTestCase):
    def ready(self, congling, *, admins=None, sessions=("1001", "1002")):
        provider = CapturingProvider(VALID_PROBE_RESPONSE)
        guard = plugin(gate_config(
            classifier_outage_recovery_enabled=True,
            classifier_provider_id="provider-a",
        ), provider)
        guard.context = CrossPluginContext(provider, congling, admins)
        guard._ensure_classifier_probe_runtime()
        guard._classifier_probe_platform_id = "qq-bot"
        outage = guard._outage()
        event = outage.create_event(
            "timeout", "provider-a", at=time.time() - COOLDOWN_SECONDS - 1
        )
        for session in sessions:
            outage.add_affected_session(
                f"qq-bot:GroupMessage:{session}", at=time.time() - 300
            )
        outage.transition(OutageState.PROBING)
        work = outage.complete_probe_success(event.event_id)
        return guard, event, work

    async def test_livinghello_calls_congling_and_closes_completed_batch(self):
        congling = SimpleNamespace()
        congling.recover_classifier_outage = AsyncMock()
        guard, event, work = self.ready(congling, admins=["9001"])
        observed = []
        guard.register_classifier_recovery_callback(observed.append)
        congling.recover_classifier_outage.return_value = {
            "contract": "congling.classifier-recovery.v1",
            "event_id": work.event_id,
            "sessions": [
                {
                    "session_id": work.sessions[0].session_id,
                    "status": "completed",
                    "dedupe_key": "a" * 64,
                },
                {
                    "session_id": work.sessions[1].session_id,
                    "status": "no_reply",
                    "dedupe_key": "",
                },
            ],
        }

        self.assertTrue(await guard.resume_classifier_recovery())

        congling.recover_classifier_outage.assert_awaited_once_with(work)
        self.assertEqual(observed, [work])
        self.assertEqual(
            event.affected_sessions[work.sessions[0].session_id].backfill_status,
            BackfillStatus.COMPLETED,
        )
        self.assertEqual(
            event.affected_sessions[work.sessions[1].session_id].backfill_status,
            BackfillStatus.NO_REPLY,
        )
        self.assertEqual(guard._outage().state, OutageState.HEALTHY)
        self.assertIsNone(guard._outage().current_event)
        self.assertEqual(
            [route for route, _ in guard.context.sent],
            ["qq-bot:FriendMessage:9001"],
        )
        self.assertIn("成功 1，无需回复 1，待重试 0", guard.context.sent[0][1])

    async def test_congling_unavailable_only_notifies_admin(self):
        guard, event, work = self.ready(None, admins=["9001"])

        await guard._publish_classifier_recovery(work)

        self.assertTrue(all(
            session.backfill_status is BackfillStatus.UNAVAILABLE
            for session in event.affected_sessions.values()
        ))
        self.assertEqual(guard._outage().state, OutageState.HEALTHY)
        self.assertEqual(len(guard.context.sent), 1)
        route, text = guard.context.sent[0]
        self.assertEqual(route, "qq-bot:FriendMessage:9001")
        self.assertIn("不可用 2", text)
        self.assertTrue(all(":FriendMessage:" in item[0]
                            for item in guard.context.sent))

    async def test_retry_and_unknown_results_keep_event_recovering(self):
        congling = SimpleNamespace()
        congling.recover_classifier_outage = AsyncMock()
        guard, event, work = self.ready(
            congling, admins=["9001"], sessions=("1001",)
        )
        congling.recover_classifier_outage.return_value = {
            "contract": "congling.classifier-recovery.v1",
            "event_id": work.event_id,
            "sessions": [{
                "session_id": work.sessions[0].session_id,
                "status": "send_unknown",
                "dedupe_key": "b" * 64,
            }],
        }

        await guard._publish_classifier_recovery(work)

        self.assertEqual(guard._outage().state, OutageState.RECOVERING)
        self.assertIs(guard._outage().current_event, event)
        self.assertEqual(
            event.affected_sessions[work.sessions[0].session_id].backfill_status,
            BackfillStatus.SEND_UNKNOWN,
        )
        self.assertEqual(guard.context.sent, [])

    async def test_retry_waits_ten_minutes_and_reuses_admin_throttle(self):
        congling = SimpleNamespace()
        attempts = 0

        async def recover(current):
            nonlocal attempts
            attempts += 1
            return {
                "contract": "congling.classifier-recovery.v1",
                "event_id": current.event_id,
                "sessions": [{
                    "session_id": current.sessions[0].session_id,
                    "status": (
                        "retry_pending" if attempts == 1 else "completed"
                    ),
                    "dedupe_key": "c" * 64,
                }],
            }

        congling.recover_classifier_outage = AsyncMock(side_effect=recover)
        guard, event, work = self.ready(
            congling, admins=["9001"], sessions=("1001",)
        )
        now = [time.time()]
        guard._outage().clock = lambda: now[0]
        guard._outage().mark_notification(
            NotificationPhase.CONTINUED,
            "9001",
            cycle=1,
            at=now[0],
        )

        await guard._publish_classifier_recovery(work)

        session = event.affected_sessions[work.sessions[0].session_id]
        self.assertEqual(session.backfill_status, BackfillStatus.RETRY_PENDING)
        self.assertEqual(
            session.retry_not_before, now[0] + COOLDOWN_SECONDS
        )
        self.assertEqual(attempts, 1)
        self.assertEqual(guard.context.sent, [])
        self.assertFalse(await guard.resume_classifier_recovery())
        now[0] += COOLDOWN_SECONDS - 1
        self.assertFalse(await guard.resume_classifier_recovery())
        self.assertEqual(attempts, 1)

        now[0] += 1
        self.assertTrue(await guard.resume_classifier_recovery())

        self.assertEqual(attempts, 2)
        self.assertEqual(guard._outage().state, OutageState.HEALTHY)
        self.assertEqual(len(guard.context.sent), 1)
        self.assertIn("LivingHello 文字判定状态：恢复", guard.context.sent[0][1])
        await guard.terminate()

    async def test_cancelled_handoff_persists_retry_cooldown(self):
        congling = SimpleNamespace()
        congling.recover_classifier_outage = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        guard, event, work = self.ready(
            congling, sessions=("1001",)
        )
        now = [time.time()]
        guard._outage().clock = lambda: now[0]

        with self.assertRaises(asyncio.CancelledError):
            await guard._publish_classifier_recovery(work)

        session = event.affected_sessions[work.sessions[0].session_id]
        self.assertEqual(session.backfill_status, BackfillStatus.RETRY_PENDING)
        self.assertEqual(
            session.retry_not_before, now[0] + COOLDOWN_SECONDS
        )
        self.assertFalse(await guard.resume_classifier_recovery())
        congling.recover_classifier_outage.assert_awaited_once()
        await guard.terminate()

    async def test_concurrent_duplicate_handoff_calls_congling_once(self):
        congling = SimpleNamespace()

        async def recover(current):
            await asyncio.sleep(0.02)
            return {
                "contract": "congling.classifier-recovery.v1",
                "event_id": current.event_id,
                "sessions": [{
                    "session_id": current.sessions[0].session_id,
                    "status": "completed",
                    "dedupe_key": "d" * 64,
                }],
            }

        congling.recover_classifier_outage = AsyncMock(side_effect=recover)
        guard, _, _ = self.ready(congling, sessions=("1001",))

        results = await asyncio.gather(
            guard.resume_classifier_recovery(),
            guard.resume_classifier_recovery(),
        )

        self.assertEqual(results, [True, True])
        congling.recover_classifier_outage.assert_awaited_once()
        self.assertEqual(guard._outage().state, OutageState.HEALTHY)


if __name__ == "__main__":
    unittest.main()
