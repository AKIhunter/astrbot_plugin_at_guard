"""主回复方式：确定性引用/@与Emoji清理；断句和媒体处理遵循宿主。"""
from __future__ import annotations

import re

from astrbot.api.message_components import At, Plain, Reply


def takeover(config):
    return config.get("expression_takeover_enabled", True) is True


def congling_for(context, event):
    """每次解析已激活实例及会话开关，避免重载后缓存旧实例。"""
    for name in ("astrbot_plugin_congling", "congling"):
        try:
            metadata = context.get_registered_star(name)
            plugin = getattr(metadata, "star_cls", None)
            if (getattr(metadata, "activated", False) and plugin is not None
                    and callable(getattr(plugin, "enabled_for", None))
                    and plugin.enabled_for(event)):
                return plugin
        except Exception:
            continue
    return None


class Expression:
    def __init__(self, plugin):
        self.plugin = plugin
        self.config = plugin.config

    def clean(self, text):
        if self.config.get("expression_strip_emoji", True):
            text = re.sub("[\U0001f000-\U0001faff\u2600-\u27bf\ufe00-\ufe0f\u200d]+", "", text)
            text = re.sub(r"[（(][^（）()]{0,12}[ω・∀▽｀´°￣][^（）()]{0,12}[)）]", "", text)
        # 换行、标点、代码与模型原字均保留，不暗藏其他表达规则。
        return text

    async def decorate(self, event, result):
        if not takeover(self.config) or not getattr(result, "is_llm_result", lambda: False)():
            return
        self.clean_chain(result)

    def clean_chain(self, result):
        result.chain = [
            Plain(self.clean(c.text)) if isinstance(c, Plain) else c for c in result.chain or []
        ]
        result.chain = [c for c in result.chain if not isinstance(c, Plain) or c.text.strip()]

    def before_send(self, event, chain, plan):
        """仅调整当前主回复自动头；原有引用及指向其他人的@保留。"""
        chain = list(chain)
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        sender = str(event.get_sender_id() or "")
        semantic = plan["semantic_headers"]
        chain = [c for c in chain if id(c) in semantic or not (
            isinstance(c, Reply) and str(c.id) == mid
            or isinstance(c, At) and str(c.qq) == sender)]
        body = [c for c in chain if not isinstance(c, (Reply, At))]
        # 语音由原生单独发送，不消费首条可承载回复头的机会。
        if not body or all(type(c).__name__ == "Record" for c in body):
            return chain
        if plan["header_sent"]:
            return chain
        heads = []
        if (self.config.get("reply_quote_enabled", True) is True
                and mid and mid not in {"0", "None", "null"}
                and not any(isinstance(c, Reply) for c in chain)):
            heads.append(Reply(id=mid))
        platform = getattr(event, "get_platform_name", lambda: "")()
        if (self.config.get("reply_mention_enabled", False) is True and event.get_group_id()
                and platform not in {"qq_official_webhook", "weixin_official_account"}
                and sender and sender not in {"0", "None", "null"}
                and not any(isinstance(c, At) and str(c.qq) == sender for c in chain)):
            heads.append(At(qq=sender, name=event.get_sender_name()))
        # 引用优先于@；保留原组件相对顺序和正文中其他人的@。
        if heads and isinstance(heads[0], Reply):
            return heads + chain
        if heads and chain and isinstance(chain[0], Reply):
            return [chain[0], *heads, *chain[1:]]
        return heads + chain
