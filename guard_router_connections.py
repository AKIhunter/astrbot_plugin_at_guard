"""读取多API路由的服务端连接；不把非对话API注册为聊天Provider。"""
from collections.abc import Mapping

CHAT_TYPES = {"chat", "image_to_text", "multimodal"}


def router_connections(context):
    getter = getattr(context, "get_registered_star", None)
    if not callable(getter):
        return {}
    try:
        metadata = getter("astrbot_plugin_multi_api_router")
    except Exception:
        return {}
    if not metadata or not getattr(metadata, "activated", False):
        return {}
    instance = getattr(metadata, "star_cls", None)
    config = getattr(instance, "config", None)
    if config is None:
        config = getattr(metadata, "config", None)
    if not isinstance(config, Mapping) or not config.get("enabled", True):
        return {}
    parent = str(config.get("provider_id") or "multi_api_router").strip()
    result = {}
    # 按已保存槽位发现连接，route_enabled只决定自动聊天路由，不禁用独立能力。
    for field, raw in config.items():
        if not str(field).startswith("upstream_") or not isinstance(raw, Mapping):
            continue
        slot = str(field)[len("upstream_"):]
        if not slot.isdigit() or not 1 <= int(slot) <= 100:
            continue
        base = str(raw.get("api_base") or "").strip().rstrip("/")
        key = str(raw.get("api_key") or "").strip()
        model = str(raw.get("model") or "").strip()
        if not base or not key or not model:
            continue
        pid = f"{parent}:upstream_{int(slot)}"
        result[pid] = dict(id=pid, parent=parent,
                          display=str(raw.get("name") or f"上游 {slot}").strip(),
                          model=model, api_type=str(raw.get("api_type") or "chat").lower(),
                          api_base=base, key=[key])
    return result
