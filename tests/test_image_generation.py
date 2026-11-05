"""独立生图的配置/共享额度/重复与取消，以及本地HTTP协议回归。"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Image
from guard_image_settings import ImageSettings, MAX_IMAGE_BYTES
from guard_native_reply import NativeReplyError


class Event:
    unified_msg_origin="test:group:one"
    def __init__(self,mid="message-one",images=None):
        self.message_obj=SimpleNamespace(message_id=mid)
        self.extra={}
        self.images=images or []
        self.send=AsyncMock()
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def get_sender_id(self):return "user-one"
    def get_messages(self):return self.images
    def chain_result(self,chain):return SimpleNamespace(chain=chain)


class ImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.native=SimpleNamespace(provider_config=dict(id="native-image",type="openai",
            api_base="https://example.invalid/v1",key=["secret-marker"]),
            get_model=lambda:"image-one")
        self.router=SimpleNamespace(provider_config=dict(id="router",type="multi_api_router"),
            upstreams=[SimpleNamespace(client=self.native,slot=2,name="绘图上游")])
        self.context=SimpleNamespace(get_all_providers=lambda:[self.native,self.router],
                                     get_registered_star=lambda _:self.fail("不得依赖BiliBot"))
        self.plugin=SimpleNamespace(context=self.context,data_dir=Path(self.folder.name),
            _delivery=lambda:SimpleNamespace(recalled=lambda e:False),
            _judged_event=lambda e:False,_gate_mode=lambda:"enforce")
        self.service=ImageSettings(self.plugin)
        self.service.ensure_tool=lambda:object()
        self.service.values.update(enabled=True,provider_id="native-image",daily_limit=2)
        self.output=Path(self.folder.name)/"generated.png"
        self.output.write_bytes(b"\x89PNG\r\n\x1a\n"+b"test-image")
        self.service._request=AsyncMock(return_value=self.output)

    def body(self,**values):
        return dict(expected_revision=self.service.read()["revision"],values=values)

    def test_public_targets_include_router_and_never_secrets(self):
        payload=self.service.read()
        self.assertEqual({p["id"] for p in payload["providers"]},{"native-image","router:upstream_2"})
        self.assertNotIn("secret-marker",json.dumps(payload))
        self.assertNotIn("api_base",json.dumps(payload))
        self.assertEqual(self.service.connection({**self.service.values,
            "provider_id":"router:upstream_2"})[2],"image-one")

    async def test_typed_image_router_outside_chat_registry_can_save_and_reload(self):
        config = dict(enabled=True, provider_id="typed-router",
                      upstream_7=dict(api_type="image_generation", name="专用生图API",
                                      api_base="https://images.invalid/v1", api_key="typed-secret",
                                      model="image-seven", route_enabled=False),
                      upstream_8=dict(api_type="embedding", name="向量API",
                                      api_base="https://embedding.invalid/v1", api_key="embedding-secret",
                                      model="embedding-eight"))
        self.context.get_registered_star = lambda name: SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(config=config)) if name == "astrbot_plugin_multi_api_router" else None
        self.context.get_all_providers = lambda: [self.native]  # 生图上游没有聊天Provider。
        result = await self.service.save(self.body(provider_id="typed-router:upstream_7"))
        row = next(p for p in result["providers"] if p["id"] == "typed-router:upstream_7")
        self.assertEqual((row["display"], row["model"]), ("专用生图API", "image-seven"))
        self.assertNotIn("typed-router:upstream_8", {p["id"] for p in result["providers"]})
        self.assertNotIn("typed-secret", json.dumps(result))
        self.assertEqual(self.service.connection(self.service.values),
                         ("https://images.invalid/v1", "typed-secret", "image-seven"))
        replacement = ImageSettings(self.plugin)
        self.assertEqual(replacement.values["provider_id"], "typed-router:upstream_7")
        config["enabled"] = False
        with self.assertRaises(NativeReplyError):
            replacement.connection(replacement.values)

    async def test_existing_custom_size_and_extended_timeout_survive_reload(self):
        await self.service.save(self.body(size="2048x2048", timeout_seconds=240))
        replacement = ImageSettings(self.plugin)
        self.assertFalse(replacement.error)
        self.assertEqual(replacement.values["size"], "2048x2048")
        self.assertEqual(replacement.values["timeout_seconds"], 240)
        for patch in (dict(size="2049x2048"), dict(size="511x1024"), dict(timeout_seconds=501)):
            with self.assertRaises(NativeReplyError):
                await self.service.save(self.body(**patch))

    def test_dashscope_image_response_is_preserved(self):
        self.assertEqual(ImageSettings.extract({"output": {"choices": [{"message": {
            "content": [{"type": "image", "image": "https://example.invalid/image.png"}]}}]}}),
            "https://example.invalid/image.png")

    async def test_atomic_save_reload_and_stale_revision(self):
        before=self.service.read()["revision"]
        await self.service.save(self.body(model="image-two",daily_limit=10))
        new=ImageSettings(self.plugin)
        self.assertEqual(new.values["model"],"image-two")
        with self.assertRaises(NativeReplyError):
            await self.service.save(dict(expected_revision=before,values={"daily_limit":20}))

    async def test_bad_model_unknown_field_and_bounds_rejected(self):
        for values in (dict(provider_id="missing"),dict(api_key="cannot-edit"),
                       dict(daily_limit=-1),dict(daily_limit=True),dict(protocol={}),
                       dict(timeout_seconds=9),dict(size="huge")):
            with self.assertRaises(NativeReplyError):
                await self.service.save(self.body(**values))
        self.assertFalse(self.service.path.exists())

    async def test_disk_failure_preserves_running_configuration(self):
        before=dict(self.service.values)
        with patch.object(self.service,"_write",side_effect=OSError("fixture")):
            with self.assertRaises(NativeReplyError):
                await self.service.save(self.body(daily_limit=9))
        self.assertEqual(self.service.values,before)

    async def test_duplicate_event_does_not_call_or_send_twice(self):
        event=Event()
        self.assertIn("已发送",await self.service.generate(event,"画一只猫"))
        self.assertIn("已请求过",await self.service.generate(event,"画一只猫"))
        self.assertEqual(self.service._request.await_count,1)
        self.assertEqual(event.send.await_count,1)
        self.assertEqual(self.service.used(),1)

    async def test_concurrent_calls_respect_global_daily_limit(self):
        self.service.values["daily_limit"]=1
        results=await asyncio.gather(*(self.service.generate(Event(str(i)),"画猫") for i in range(8)))
        self.assertEqual(sum("已发送" in r for r in results),1)
        self.assertEqual(self.service._request.await_count,1)
        self.assertEqual(self.service.used(),1)

    def test_ledger_shared_across_instances_and_reload(self):
        self.service.values["daily_limit"]=1
        self.service.reserve(Event("first"),"画猫",False)
        new=ImageSettings(self.plugin)
        new.values.update(daily_limit=1)
        self.assertEqual(new.used(),1)
        with self.assertRaises(NativeReplyError):
            new.reserve(Event("second"),"画狗",False)
        with patch.object(new,"today",return_value="2099-01-01"):
            self.assertEqual(new.used(),0)
            self.assertTrue(new.reserve(Event("tomorrow"),"画猫",False))

    async def test_upstream_failure_keeps_quota_and_does_not_retry(self):
        self.service._request.side_effect=TimeoutError("secret-marker")
        result=await self.service.generate(Event(),"画猫")
        self.assertNotIn("secret-marker",result)
        self.assertEqual(self.service.used(),1)
        self.assertEqual(self.service._request.await_count,1)

    async def test_missing_reference_never_requests_or_consumes(self):
        self.assertIn("补充图片",await self.service.generate(Event(),"换背景",True))
        self.service._request.assert_not_awaited()
        self.assertEqual(self.service.used(),0)

    async def test_reference_is_taken_from_current_event(self):
        event=Event(images=[Image(file=str(self.output))])
        result=await self.service.generate(event,"换成蓝色",True)
        self.assertIn("已发送",result)
        reference=self.service._request.await_args.args[-1]
        self.assertEqual(reference[0],self.output.read_bytes())

    async def test_cancelled_after_generation_never_sends(self):
        event=Event()
        async def generate(*_):
            event.set_extra("_at_guard_delivery_blocked","context_changed")
            return self.output
        self.service._request.side_effect=generate
        self.assertIn("未发送",await self.service.generate(event,"画猫"))
        event.send.assert_not_awaited()
        self.assertEqual(self.service.used(),1)

    async def test_disabled_and_zero_limit_make_no_request(self):
        self.service.values["enabled"]=False
        await self.service.generate(Event(),"画猫")
        self.service.values.update(enabled=True,daily_limit=0)
        await self.service.generate(Event("another"),"画猫")
        self.service._request.assert_not_awaited()

    async def test_disabling_while_generating_suppresses_delivery(self):
        event=Event()
        async def generate(*_):
            self.service.values["enabled"]=False
            return self.output
        self.service._request.side_effect=generate
        self.assertIn("未发送",await self.service.generate(event,"画猫"))
        event.send.assert_not_awaited()
        self.assertEqual(self.service.used(),1)

    async def test_shutdown_cancels_inflight_without_refunding_or_sending(self):
        event=Event()
        started=asyncio.Event()
        async def generate(*_):
            started.set()
            await asyncio.Event().wait()
        self.service._request.side_effect=generate
        task=asyncio.create_task(self.service.generate(event,"画猫"))
        await started.wait()
        await self.service.close()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.service.used(),1)
        event.send.assert_not_awaited()

    async def test_pre_cancel_and_corrupt_quota_fail_closed(self):
        event=Event()
        event.set_extra("_at_guard_delivery_blocked","message_recalled")
        await self.service.generate(event,"画猫")
        self.service.ledger.write_bytes(b"not sqlite")
        self.assertFalse(self.service.read()["available"])
        await self.service.generate(Event("new"),"画猫")
        self.service._request.assert_not_awaited()

    def test_decode_rejects_nonimage_and_handles_standard_chat_shapes(self):
        image=base64.b64encode(self.output.read_bytes()).decode()
        for payload in ({"data":[{"b64_json":image}]},
                        {"choices":[{"message":{"images":[{"image_url":{"url":"data:image/png;base64,"+image}}]}}]}):
            value=ImageSettings.extract(payload)
            self.assertEqual(ImageSettings._image_bytes(value)[0],self.output.read_bytes())
        for value in ("not base64",base64.b64encode(b"<html>not image</html>").decode()):
            with self.assertRaises(NativeReplyError):ImageSettings._image_bytes(value)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"),"HTTP协议测试需宿主已有aiohttp")
class ImageHttpTests(unittest.IsolatedAsyncioTestCase):
    setUp = ImageGenerationTests.setUp
    async def asyncSetUp(self):
        from aiohttp import web
        self.received=[]
        self.http_status = 200
        self.response_payload = None
        async def handler(request):
            if request.content_type=="multipart/form-data":
                form=await request.post()
                self.received.append((request.path,dict(model=form["model"],n=form["n"],
                                                      image=form["image"].file.read())))
            else:self.received.append((request.path,await request.json()))
            return web.json_response(self.response_payload or
                {"data":[{"b64_json":base64.b64encode(self.output.read_bytes()).decode()}]},
                status=self.http_status)
        app=web.Application()
        app.router.add_post("/v1/images/generations",handler)
        app.router.add_post("/v1/images/edits",handler)
        app.router.add_post("/v1/chat/completions",handler)
        async def download(request):
            return web.Response(status=503)
        app.router.add_get("/image.png", download)
        self.runner=web.AppRunner(app)
        await self.runner.setup()
        site=web.TCPSite(self.runner,"127.0.0.1",0)
        await site.start()
        self.base="http://127.0.0.1:"+str(site._server.sockets[0].getsockname()[1])+"/v1"

    async def asyncTearDown(self):await self.runner.cleanup()

    async def test_generation_edit_and_chat_requests_use_real_http_protocol(self):
        values=self.service.values
        connection=(self.base,"local-fixture-key","image-model")
        for reference in (None,(self.output.read_bytes(),"image/png")):
            generated=await ImageSettings._request(self.service,values,connection,"画猫",reference)
            self.assertEqual(generated.read_bytes(),self.output.read_bytes())
        values={**values,"protocol":"chat_images"}
        await ImageSettings._request(self.service,values,connection,"换背景",
                                     (self.output.read_bytes(),"image/png"))
        self.assertEqual([r[0] for r in self.received],
                         ["/v1/images/generations","/v1/images/edits","/v1/chat/completions"])
        self.assertEqual(self.received[0][1]["n"],1)
        self.assertEqual(self.received[1][1]["image"],self.output.read_bytes())
        self.assertEqual(self.received[2][1]["modalities"],["image"])

    async def test_trace_counts_real_http_api_and_successful_image_delivery(self):
        from guard_request_trace import RequestTrace, ID_KEY
        trace = RequestTrace(self.plugin, self.plugin.data_dir)
        event = Event()
        trace.begin(event)
        self.service.connection = lambda values: (self.base, "fixture-key", "image-model")
        self.service._request = lambda *args: ImageSettings._request(self.service, *args)
        try:
            result = await self.service.generate(event, "仅本地HTTP生图")
            row = await trace.get(event.get_extra(ID_KEY))
            self.assertIn("图片已发送", result)
            self.assertEqual(row["summary"]["image_api_calls"], 1)
            self.assertEqual(row["summary"]["tool_calls"], 1)
            self.assertEqual(row["features"]["image_api"]["status"], "success")
            self.assertEqual(row["features"]["image_delivery"]["status"], "success")
            self.assertEqual(len(self.received), 1)
        finally:
            await trace.close()

    async def test_trace_distinguishes_http_failure_from_download_failure(self):
        from guard_request_trace import RequestTrace, ID_KEY
        trace = RequestTrace(self.plugin, self.plugin.data_dir)
        self.service.connection = lambda values: (self.base, "fixture-key", "image-model")
        self.service._request = lambda *args: ImageSettings._request(self.service, *args)
        try:
            self.http_status = 503
            first = Event(mid="http-failure")
            trace.begin(first)
            await self.service.generate(first, "API失败")
            row = await trace.get(first.get_extra(ID_KEY))
            self.assertEqual(row["features"]["image_api"]["status"], "failed")
            self.assertEqual(row["summary"]["image_api_calls"], 1)
            self.http_status = 200
            self.response_payload = {"data": [{"url": self.base.removesuffix("/v1") + "/image.png"}]}
            second = Event(mid="download-failure")
            trace.begin(second)
            await self.service.generate(second, "下载失败")
            row = await trace.get(second.get_extra(ID_KEY))
            self.assertEqual(row["features"]["image_api"]["status"], "success")
            self.assertEqual(row["features"]["image_download"]["status"], "failed")
            self.assertEqual(row["summary"]["image_api_calls"], 1)
            self.assertNotIn("image_delivery", row["features"])
            self.assertEqual(self.service.used(), 2)
        finally:
            await trace.close()
