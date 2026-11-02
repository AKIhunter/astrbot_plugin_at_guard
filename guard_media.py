"""图片判定模型选择：尊重宿主能力声明，路由始终锁定一个视觉上游。"""
import re

try:
    from .guard_engine import GateFailure, GateProvider
except ImportError:
    from guard_engine import GateFailure, GateProvider


def supports_images(provider, model=""):
    config = getattr(provider, "provider_config", {}) or {}
    getter = getattr(provider, "get_model", None)
    name = (model or (getter() if callable(getter) else "") or "").lower()
    # 已知纯文本型号不因宿主默认勾选全部modalities变成视觉模型。
    if (("deepseek" in name and not re.search(r"deepseek[-/]vl", name))
            or ("qwen" in name and "vl" not in name)
            or "hunyuanmt" in name or "embedding" in name):
        return False
    modalities = config.get("modalities")
    if isinstance(modalities, list) and modalities:
        # 宿主显式关闭图片时，不能仅靠模型名称覆盖该设置。
        return "image" in [str(value).lower() for value in modalities]
    explicit = config.get("supports_vision")
    if isinstance(explicit, bool):
        return explicit
    # 没有能力字段时只认明确视觉型号，不能把全部主流品牌都当成多模态。
    return bool(re.search(
        r"qwen[^/]*(?:-vl|2\.5-vl)|gemini|claude-(?:3|sonnet-4|opus-4|haiku-4)"
        r"|gpt-4o|gpt-4\.1|gpt-5(?!.*(?:codex|text))|glm-[^-]*v|internvl|pixtral"
        r"|vision|deepseek-vl", name))


class StrictVisionProvider:
    """OpenAI宿主默认会在视觉报错后删图重试，闸门必须把该异常交给下一层。"""

    def __init__(self, provider):
        self.provider = provider

    async def text_chat(self, **kwargs):
        payload, _ = await self.provider._prepare_chat_payload(
            kwargs["prompt"], kwargs.get("image_urls"), None, [],
            kwargs["system_prompt"], None, model=kwargs.get("model"),
        )
        payload["max_tokens"] = kwargs.get("max_tokens", 900)
        return await self.provider._query(payload, None, request_max_retries=1)


def image_target(provider, label=""):
    if provider is None:
        raise GateFailure("no_vision_provider")
    config = getattr(provider, "provider_config", {}) or {}
    provider_id = str(config.get("id", "") or type(provider).__name__)
    pinned = label.split("::", 1)[1].strip() if "::" in label else ""
    upstreams = getattr(provider, "upstreams", None)
    if isinstance(upstreams, (list, tuple)):
        # 不把图片交给未锁定的混合路由，绕开“找不到pin就走全部上游”的行为。
        choices = []
        for upstream in upstreams:
            client = getattr(upstream, "client", None)
            if client is None:
                continue
            model = str(client.get_model() or "")
            if pinned and pinned != model:
                continue
            if supports_images(client, model):
                choices.append(model)
        if not choices:
            raise GateFailure("main_no_vision" if not label else "no_vision_provider")
        model = choices[0]
    else:
        model = pinned or str(provider.get_model() or "")
        if not supports_images(provider, model):
            raise GateFailure("main_no_vision" if not label else "no_vision_provider")
        # 不改变共享provider；此局部适配器仅用于图片参与分类。
        if (getattr(provider, "provider_config", {}).get("type") == "openai_chat_completion"
                and callable(getattr(provider, "_prepare_chat_payload", None))
                and callable(getattr(provider, "_query", None))):
            provider = StrictVisionProvider(provider)
    return GateProvider(provider, model, f"{provider_id}::{model}")
