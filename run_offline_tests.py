"""无AstrBot安装时用最小框架替身执行真实插件逻辑；不会接触部署或平台。"""
import enum
import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest


def install_framework_stubs():
    def mod(name):
        result = types.ModuleType(name)
        result.__path__ = []
        sys.modules[name] = result
        return result

    mod("astrbot")
    api = mod("astrbot.api")
    api.logger = logging.getLogger("at-guard-offline")
    events = mod("astrbot.api.event")
    events.AstrMessageEvent = object

    class Filters:
        EventMessageType = types.SimpleNamespace(ALL="all", GROUP_MESSAGE="group")
        def __getattr__(self, _):
            return lambda *args, **kwargs: lambda function: function
    events.filter = Filters()
    star = mod("astrbot.api.star")
    star.Context = star.Star = object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda *_: None)
    provider = mod("astrbot.api.provider")
    provider.LLMResponse = provider.ProviderRequest = object
    components = mod("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text
    class At:
        def __init__(self, qq="", name=""):
            self.qq, self.name = qq, name
    class Image:
        def __init__(self, url="", file="", path=""):
            self.url, self.file, self.path = url, file, path
        @classmethod
        def fromURL(cls, url):
            return cls(url=url, file=url)
        @classmethod
        def fromFileSystem(cls, path):
            return cls(file=path, path=path)
    class Record(Image):
        def __init__(self, url="", file="", path="", text=""):
            super().__init__(url=url, file=file, path=path)
            self.text = text
    class Reply:
        def __init__(self, id="", sender_id="", sender_nickname="", message_str="",
                     text="", chain=None, time=0, **kwargs):
            self.id, self.sender_id, self.sender_nickname = id, sender_id, sender_nickname
            self.message_str, self.text, self.chain, self.time = message_str, text, chain or [], time
    components.Plain, components.At, components.Image, components.Reply = Plain, At, Image, Reply
    components.Record = Record
    mod("astrbot.core")
    mod("astrbot.core.agent")
    message = mod("astrbot.core.agent.message")
    class TextPart(Plain):
        def mark_as_temp(self):
            self.no_save = True
            return self
    message.TextPart = TextPart
    mod("astrbot.core.platform")
    message_type = mod("astrbot.core.platform.message_type")
    class MessageType(enum.Enum):
        GROUP_MESSAGE = "group"
        FRIEND_MESSAGE = "friend"
    message_type.MessageType = MessageType
    web = mod("astrbot.api.web")
    web.request = types.SimpleNamespace()
    web.json_response = lambda data: data
    web.error_response = lambda message, **_: message


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    if importlib.util.find_spec("astrbot") is None:
        install_framework_stubs()
    root = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(root), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(not result.wasSuccessful())
