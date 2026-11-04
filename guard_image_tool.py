"""原生 FunctionTool 包装，供当前主模型按需选择文生图或图生图。"""
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext


@dataclass
class GenerateImageTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}
    service: Any = None
    name: str = "livinghello_generate_image"
    description: str = (
        "根据本次用户请求生成或编辑一张图片，并发送到当前会话。"
        "仅在用户请求或当前回复确需配图时调用；图生图必须使用本次消息或引用中的图片。"
        "有每日共享额度，失败或结果未知不能自动重试。"
    )
    parameters: dict = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "所需画面或编辑要求，1~4000字"},
            "use_reference": {"type": "boolean", "description": "true使用本次输入图片做图生图；false文生图"},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    })

    async def call(self, context, **kwargs):
        node = context
        for _ in range(5):
            if callable(getattr(node, "get_sender_id", None)):
                break
            node = getattr(node, "event", None) or getattr(node, "context", None)
            if node is None:
                return "缺少当前会话，不能生图或发送图片。"
        if not callable(getattr(node, "get_sender_id", None)) or self.service is None:
            return "缺少当前会话或生图能力。"
        return await self.service.generate(node, kwargs.get("prompt"), kwargs.get("use_reference", False))
