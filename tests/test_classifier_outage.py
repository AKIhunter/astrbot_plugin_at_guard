import json
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from guard_classifier_outage import (
    COOLDOWN_SECONDS,
    LEGAL_TRANSITIONS,
    BackfillStatus,
    ClassifierOutageRecovery,
    FailureDisposition,
    InvalidOutageTransition,
    NotificationPhase,
    OutageState,
    OutageStateMachine,
    VALID_STATES,
    classify_classifier_failure,
    failure_category_label,
    redacted_provider_id,
)


ROOT = Path(__file__).resolve().parents[1]


class OutageStateTests(unittest.TestCase):
    def test_schema_defaults_off_and_cooldown_is_fixed_in_code(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        field = schema["classifier_outage_recovery_enabled"]
        self.assertEqual(field["type"], "bool")
        self.assertIs(field["default"], False)
        self.assertEqual(COOLDOWN_SECONDS, 600)
        self.assertFalse(any("cooldown" in key and "outage" in key for key in schema))

    def test_states_and_exact_legal_transitions(self):
        self.assertEqual(
            VALID_STATES,
            {
                "disabled",
                "healthy",
                "cooling",
                "probing",
                "recovering",
                "needs_attention",
            },
        )
        expected = {
            OutageState.DISABLED: {OutageState.HEALTHY},
            OutageState.HEALTHY: {
                OutageState.COOLING,
                OutageState.NEEDS_ATTENTION,
                OutageState.DISABLED,
            },
            OutageState.COOLING: {OutageState.PROBING},
            OutageState.PROBING: {
                OutageState.COOLING,
                OutageState.RECOVERING,
                OutageState.NEEDS_ATTENTION,
            },
            OutageState.RECOVERING: {OutageState.HEALTHY},
            OutageState.NEEDS_ATTENTION: {OutageState.PROBING},
        }
        self.assertEqual(LEGAL_TRANSITIONS, expected)
        for source, targets in expected.items():
            for target in OutageState:
                machine = OutageStateMachine(source)
                if target in targets:
                    self.assertEqual(machine.transition(target), target)
                else:
                    with self.assertRaises(InvalidOutageTransition):
                        machine.transition(target)

    def test_disabled_service_cannot_create_an_event(self):
        service = ClassifierOutageRecovery({}, None, logging.getLogger())
        self.assertEqual(service.state, OutageState.DISABLED)
        with self.assertRaises(InvalidOutageTransition):
            service.create_event("timeout", "provider-a", at=100)

    def test_invalid_event_does_not_advance_state(self):
        service = ClassifierOutageRecovery(
            {"classifier_outage_recovery_enabled": True}, None, logging.getLogger()
        )
        with self.assertRaises(ValueError):
            service.create_event("timeout: 含正文", "provider-a", at=100)
        self.assertEqual(service.state, OutageState.HEALTHY)
        self.assertIsNone(service.current_event)


class OutageFailureClassificationTests(unittest.TestCase):
    class HttpError(RuntimeError):
        def __init__(self, status_code):
            self.status_code = status_code
            super().__init__("must not be retained")

    class APITimeoutError(RuntimeError):
        pass

    def assert_failure(self, expected_category, expected_disposition, **kwargs):
        failure = classify_classifier_failure(**kwargs)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.category, expected_category)
        self.assertEqual(failure.disposition, expected_disposition)

    def test_recoverable_reason_matrix(self):
        for reason, category in (
            ("timeout", "timeout"),
            ("deadline", "timeout"),
            ("connection_error", "connection_error"),
            ("http_429", "http_429"),
            ("http_5xx", "http_5xx"),
            ("empty_response", "empty_response"),
            ("parse_error", "protocol_error"),
            ("protocol_error", "protocol_error"),
        ):
            with self.subTest(reason=reason):
                self.assert_failure(
                    category,
                    FailureDisposition.RECOVERABLE,
                    reason=reason,
                )

    def test_http_and_connection_exception_matrix(self):
        for status, category, disposition in (
            (401, "authentication_error", FailureDisposition.NEEDS_ATTENTION),
            (403, "authentication_error", FailureDisposition.NEEDS_ATTENTION),
            (400, "configuration_error", FailureDisposition.NEEDS_ATTENTION),
            (404, "configuration_error", FailureDisposition.NEEDS_ATTENTION),
            (422, "configuration_error", FailureDisposition.NEEDS_ATTENTION),
            (429, "http_429", FailureDisposition.RECOVERABLE),
            (500, "http_5xx", FailureDisposition.RECOVERABLE),
            (503, "http_5xx", FailureDisposition.RECOVERABLE),
        ):
            with self.subTest(status=status):
                self.assert_failure(
                    category,
                    disposition,
                    exception=self.HttpError(status),
                )
        self.assert_failure(
            "timeout",
            FailureDisposition.RECOVERABLE,
            exception=TimeoutError("secret response"),
        )
        self.assert_failure(
            "timeout",
            FailureDisposition.RECOVERABLE,
            exception=self.APITimeoutError("secret response"),
        )
        self.assert_failure(
            "connection_error",
            FailureDisposition.RECOVERABLE,
            exception=ConnectionResetError("secret response"),
        )

    def test_attention_reasons_and_non_failures(self):
        for reason, category in (
            ("no_provider", "provider_missing"),
            ("provider_not_found", "provider_missing"),
            ("config_error", "configuration_error"),
        ):
            with self.subTest(reason=reason):
                self.assert_failure(
                    category,
                    FailureDisposition.NEEDS_ATTENTION,
                    reason=reason,
                )
        for reason in (
            "budget",
            "model_silent",
            "others_talking",
            "conversation_ended",
            "already_answered",
            "context_missing",
            "no_value",
            "below_threshold",
            "allowed",
        ):
            with self.subTest(reason=reason):
                self.assertIsNone(classify_classifier_failure(reason))

    def test_public_labels_cannot_expose_urls_or_credentials(self):
        self.assertEqual(failure_category_label("http_429"), "请求受限")
        self.assertEqual(redacted_provider_id("router::Qwen/Qwen3"), "router::Qwen/Qwen3")
        for value in (
            "https://provider.invalid/v1?token=secret",
            "provider::sk-secret",
            "provider_api_key",
        ):
            with self.subTest(value=value):
                self.assertEqual(redacted_provider_id(value), "current_provider")


class OutagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.directory = Path(self.folder.name)
        self.config = {"classifier_outage_recovery_enabled": True}

    def tearDown(self):
        self.folder.cleanup()

    def service(self):
        return ClassifierOutageRecovery(
            self.config, self.directory, logging.getLogger("outage-test")
        )

    def test_event_sessions_notifications_and_backfill_survive_reload(self):
        service = self.service()
        event = service.create_event("timeout", "router::Qwen/Qwen3-8B", at=1000)
        self.assertEqual(service.state, OutageState.COOLING)
        self.assertEqual(event.cooldown_until, 1000 + COOLDOWN_SECONDS)

        service.add_affected_session("qq:group:1001", at=1010)
        service.add_affected_session("qq:group:1001", at=1020)
        service.set_backfill_status(
            "qq:group:1001", BackfillStatus.RETRY_PENDING, at=1030
        )
        self.assertTrue(
            service.mark_notification(
                NotificationPhase.INTERRUPTED, "admin-1", cycle=0, at=1040
            )
        )
        self.assertFalse(
            service.mark_notification(
                NotificationPhase.INTERRUPTED, "admin-1", cycle=0, at=1050
            )
        )

        restored = self.service()
        self.assertEqual(restored.state, OutageState.COOLING)
        self.assertEqual(restored.current_event.event_id, event.event_id)
        session = restored.current_event.affected_sessions["qq:group:1001"]
        self.assertEqual(session.first_affected_at, 1010)
        self.assertEqual(session.last_affected_at, 1020)
        self.assertEqual(session.backfill_status, BackfillStatus.RETRY_PENDING)
        self.assertEqual(session.retry_not_before, 1030 + COOLDOWN_SECONDS)
        self.assertEqual(len(restored.current_event.notifications), 1)
        self.assertEqual(
            restored.current_event.notifications[0].phase,
            NotificationPhase.INTERRUPTED,
        )

    def test_probe_recovery_closes_event_and_keeps_history(self):
        service = self.service()
        event_id = service.create_event(
            "connection_error", "provider-a", at=2000
        ).event_id
        service.add_affected_session("qq:private:9", at=2010)
        service.transition(OutageState.PROBING, at=2600)
        service.transition(OutageState.RECOVERING, at=2601)
        self.assertEqual(service.current_event.recovery_window_end, 2601)
        service.set_backfill_status(
            "qq:private:9", BackfillStatus.COMPLETED, at=2602
        )
        service.transition(OutageState.HEALTHY, at=2603)

        restored = self.service()
        self.assertEqual(restored.state, OutageState.HEALTHY)
        self.assertIsNone(restored.current_event)
        history = restored.store.load(event_id)
        self.assertEqual(history.state, OutageState.HEALTHY)
        self.assertEqual(history.ended_at, 2603)
        self.assertEqual(
            history.affected_sessions["qq:private:9"].backfill_status,
            BackfillStatus.COMPLETED,
        )
        self.assertEqual(restored.store.load_latest().event_id, event_id)

    def test_saved_switch_syncs_disabled_and_healthy_without_losing_events(self):
        service = ClassifierOutageRecovery(
            self.config, None, logging.getLogger("outage-switch-test")
        )
        self.assertEqual(service.state, OutageState.HEALTHY)
        self.config["classifier_outage_recovery_enabled"] = False
        self.assertEqual(service.sync_enabled(), OutageState.DISABLED)
        self.config["classifier_outage_recovery_enabled"] = True
        self.assertEqual(service.sync_enabled(), OutageState.HEALTHY)

        service.create_event("timeout", "provider-a", at=2700)
        self.config["classifier_outage_recovery_enabled"] = False
        with self.assertRaises(InvalidOutageTransition):
            service.sync_enabled()
        self.assertEqual(service.state, OutageState.COOLING)
        self.assertTrue(service.blocks_normal_calls)

    def test_probe_failure_starts_another_fixed_ten_minute_cycle(self):
        service = self.service()
        service.create_event("http_503", "provider-a", at=3000)
        service.transition(OutageState.PROBING, at=3600)
        service.transition(OutageState.COOLING, at=3601)
        self.assertEqual(
            service.current_event.cooldown_until, 3601 + COOLDOWN_SECONDS
        )

    def test_probe_token_freezes_read_only_task5_work_and_rejects_stale_results(self):
        service = self.service()
        event = service.create_event("timeout", "provider-a", at=6000)
        service.add_affected_session("qq:group:2", at=6010)
        service.add_affected_session("qq:group:1", at=6020)

        self.assertIsNone(
            service.begin_probe("provider-a", force=False, at=6599)
        )
        token = service.begin_probe("provider-b", force=False, at=6600)
        work = service.complete_probe_success(token, at=6601)

        self.assertEqual(service.state, OutageState.RECOVERING)
        self.assertEqual(work.event_id, event.event_id)
        self.assertEqual(work.window_start, 6000)
        self.assertEqual(work.window_end, 6601)
        self.assertEqual(work.provider_id, "provider-b")
        self.assertIsNone(service.current_event.cooldown_until)
        self.assertEqual(
            [session.session_id for session in work.sessions],
            ["qq:group:1", "qq:group:2"],
        )
        self.assertEqual(service.pending_recovery(), work)
        with self.assertRaises(InvalidOutageTransition):
            service.complete_probe_success(token, at=6602)
        self.assertEqual(service.current_event.recovery_window_end, 6601)

    def test_retry_cooldown_and_unknown_state_survive_reload(self):
        now = [8000.0]
        service = self.service()
        service.clock = lambda: now[0]
        event = service.create_event("timeout", "provider-a", at=7000)
        service.add_affected_session("qq:group:retry", at=7010)
        service.add_affected_session("qq:group:unknown", at=7020)
        service.transition(OutageState.PROBING, at=7600)
        service.complete_probe_success(event.event_id, at=7601)
        service.set_backfill_status(
            "qq:group:retry", BackfillStatus.RETRY_PENDING, at=8000
        )
        service.set_backfill_status(
            "qq:group:unknown", BackfillStatus.SEND_UNKNOWN, at=8000
        )

        restored = self.service()
        restored.clock = lambda: now[0]
        self.assertIsNone(restored.pending_recovery())
        retry = restored.current_event.affected_sessions["qq:group:retry"]
        unknown = restored.current_event.affected_sessions["qq:group:unknown"]
        self.assertEqual(retry.retry_not_before, 8000 + COOLDOWN_SECONDS)
        self.assertIsNone(unknown.retry_not_before)

        now[0] += COOLDOWN_SECONDS - 1
        self.assertIsNone(restored.pending_recovery())
        now[0] += 1
        work = restored.pending_recovery()
        self.assertEqual(
            [session.session_id for session in work.sessions],
            ["qq:group:retry"],
        )

    def test_attention_probe_failure_returns_to_attention_then_can_recover(self):
        service = self.service()
        service.create_event(
            "authentication_error",
            "provider-a",
            state=OutageState.NEEDS_ATTENTION,
            at=7000,
        )
        token = service.begin_probe("provider-a", force=True, at=7001)
        service.complete_probe_failure(
            token,
            classify_classifier_failure("configuration_error"),
            at=7002,
        )
        self.assertEqual(service.state, OutageState.NEEDS_ATTENTION)

        token = service.begin_probe("provider-b", force=True, at=7003)
        service.complete_probe_success(token, at=7004)
        self.assertEqual(service.state, OutageState.RECOVERING)
        self.assertEqual(service.current_event.provider_id, "provider-b")

    def test_notification_reservation_is_limited_to_once_per_ten_minutes(self):
        service = self.service()
        service.create_event("timeout", "provider-a", at=1000)
        self.assertTrue(
            service.reserve_notification(
                NotificationPhase.INTERRUPTED, "admin-1", cycle=0, at=1001
            )
        )
        self.assertFalse(
            service.reserve_notification(
                NotificationPhase.CONTINUED, "admin-1", cycle=1, at=1600
            )
        )
        self.assertTrue(
            service.reserve_notification(
                NotificationPhase.CONTINUED, "admin-1", cycle=1, at=1601
            )
        )
        self.assertTrue(
            service.reserve_notification(
                NotificationPhase.INTERRUPTED, "admin-2", cycle=0, at=1002
            )
        )

    def test_storage_schema_has_no_user_content_boundary(self):
        service = self.service()
        event = service.create_event("empty_response", "provider-a", at=4000)
        service.add_affected_session("qq:group:private-scope", at=4001)
        service.mark_notification("interrupted", "admin-2", at=4002)

        expected_columns = {
            "outage_events": {
                "event_id",
                "schema_version",
                "started_at",
                "ended_at",
                "state",
                "error_category",
                "provider_id",
                "cooldown_until",
                "recovery_window_end",
                "updated_at",
            },
            "affected_sessions": {
                "event_id",
                "session_id",
                "first_affected_at",
                "last_affected_at",
                "backfill_status",
                "updated_at",
                "retry_not_before",
            },
            "notification_receipts": {
                "event_id",
                "phase",
                "cycle",
                "recipient_id",
                "sent_at",
            },
        }
        with sqlite3.connect(service.store.path) as db:
            for table, columns in expected_columns.items():
                actual = {
                    row[1] for row in db.execute(f"PRAGMA table_info({table})")
                }
                self.assertEqual(actual, columns)
            stored = db.execute(
                "SELECT event_id, error_category, provider_id FROM outage_events"
            ).fetchone()
        self.assertEqual(stored, (event.event_id, "empty_response", "provider-a"))
        self.assertEqual(service.store.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(TypeError):
            service.add_affected_session(
                "qq:group:private-scope",
                at=4003,
                user_text="不得复制的用户正文",
            )

    def test_only_redacted_categories_and_non_url_identifiers_are_accepted(self):
        service = self.service()
        with self.assertRaises(ValueError):
            service.create_event(
                "timeout: 用户说了秘密", "provider-a", at=5000
            )
        self.assertEqual(service.state, OutageState.HEALTHY)
        with self.assertRaises(ValueError):
            service.create_event(
                "timeout", "https://secret.invalid/?token=value", at=5000
            )
        self.assertEqual(service.state, OutageState.HEALTHY)
