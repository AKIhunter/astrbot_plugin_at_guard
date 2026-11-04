# 配置参考

面向使用者与维护者：查参数、默认值及保存范围。插件默认值以 [_conf_schema.json](../_conf_schema.json) 为唯一来源；原生字段来自 [guard_native_settings.py](../guard_native_settings.py)、[guard_native_reply.py](../guard_native_reply.py)，独立生图默认值来自 [guard_image_settings.py](../guard_image_settings.py) 的 `DEFAULTS`。已核对源码：2026-11-04；字段变化时同步更新并运行校验。

## 配置保存在哪里

| 配置域 | 权威保存位置 | 生效范围 |
| --- | --- | --- |
| LivingHello 参数 | AstrBot 保存的本插件配置 | 本插件；已保存值优先于下表默认值 |
| 原生模型、人格、排版、分段 | 所选 AstrBot 原生配置 | 实际分配到该配置的会话 |
| 固定人格 | 原插件数据目录 native_persona_pins.json | 按原生配置固定，兼容已有技术 ID |
| 独立AI生图 | 本插件 image_generation.json | 全插件能力，复用所选模型连接 |

统一保存栏逐域返回结果，失败草稿保留。后端检查字段白名单、类型、范围和读取版本，共享保存锁，合并最新磁盘字段并刷新对应原生流水线。保存失败恢复本次触及字段及人格固定，不持久化运行时临时唤醒词或动态路由配置。选择原生配置只改变编辑目标；不替代 AstrBot 的会话分配。

## 插件参数

下表由 schema 默认值生成。布尔值 `true` / `false` 表示开启 / 关闭；字符串和列表用 JSON 表示，`""` 表示留空。默认提示中的成本或热态耗时属于 schema 说明，不能直接当作当前部署测量结果。

<!-- schema-defaults:start -->
| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `expression_takeover_enabled` | `true` | 启用回复方式管理 |
| `reply_quote_enabled` | `true` | 引用当前消息 |
| `reply_mention_enabled` | `false` | @当前发送者 |
| `reply_typing_enabled` | `false` | 模拟回复耗时 |
| `reply_typing_chars_per_second` | `2.5` | 模拟打字速度（字/秒） |
| `reply_recall_group` | `false` | 群聊撤回消息不回复 |
| `reply_recall_private` | `false` | 私聊撤回消息不回复 |
| `expression_strip_emoji` | `true` | 清理文本Emoji和颜文字 |
| `understand_expand_forward` | `true` | 展开合并转发 |
| `understand_forward_max_nodes` | `60` | 最多展开条数 |
| `understand_forward_max_depth` | `2` | 转发最大层数 |
| `understand_forward_summary_threshold` | `1200` | 转发摘要阈值 |
| `understand_quote_depth` | `3` | 引用链展开层数 |
| `understand_card_parse` | `true` | 解析卡片和小程序 |
| `understand_image_enabled` | `true` | 图片理解 |
| `understand_image_provider_id` | `""` | 图片理解模型 |
| `understand_voice_enabled` | `false` | 语音转文字理解 |
| `understand_voice_provider_id` | `""` | 语音理解模型 |
| `gate_mode` | `"enforce"` | 闸门模式（总开关） |
| `participation_confidence_threshold` | `65` | 普通群消息参与判断 |
| `classifier_provider_id` | `""` | 判定用的小模型 |
| `classifier_timeout_seconds` | `3.5` | 判定超时（秒） |
| `vision_provider_id` | `""` | 需要看图时的多模态小模型 |
| `gate_image_cache_ttl_seconds` | `300` | 图片观察缓存时间（秒） |
| `vision_timeout_seconds` | `6` | 多模态判定超时（秒） |
| `gate_main_fallback_enabled` | `true` | 多模态异常时使用会话主模型兜底 |
| `gate_main_fallback_timeout_seconds` | `6` | 主模型兜底超时（秒） |
| `gate_total_timeout_seconds` | `18` | 整条判定链路最长时间（秒） |
| `gate_vision_hourly_budget` | `20` | 每群每小时视觉和主模型判定上限 |
| `gate_context_messages` | `8` | 判定参考的流水条数 |
| `gate_min_length` | `2` | 最短参与长度（字） |
| `gate_context_max_age_seconds` | `300` | 上下文最长保留时间（秒） |
| `gate_reply_max_age_seconds` | `30` | 自主回复机会有效期（秒） |
| `gate_congling_prepare_compat` | `true` | 适配从绫爬楼准备耗时 |
| `gate_memory_timeout_seconds` | `1.5` | 背景记忆检索超时（秒） |
| `gate_hourly_budget` | `60` | 每群每小时判定上限 |
| `bot_names` | `["恰比姐", "恰比"]` | 机器人称呼 |
| `empty_mention_mode` | `"fixed"` | 仅 @机器人时的回复方式 |
| `empty_mention_reply` | `"嗯？有事就说。"` | 仅 @机器人时的固定文案 |
| `empty_mention_persona_prompt` | `"群友刚才只@了你，没有附带其他内容。请严格沿用当前会话已经选择的AstrBot人格，用符合该人格身份、关系、语气和说话习惯的一句简短自然话回应。不要解释任务，不要提到AI、系统、人设或提示词，也不要替用户虚构意图。"` | 仅 @机器人时的人格回复要求 |
| `apply_to_private` | `false` | 是否作用于私聊 |
| `participation_context_messages` | `12` | 参与回复的上下文条数 |
| `fragment_wait_seconds` | `3` | 碎片消息等待时间（秒） |
| `fragment_wait_private` | `false` | 私聊也等待碎片消息 |
| `proactive_cooldown_seconds` | `8` | 主动回复冷却（秒） |
| `duplicate_window_seconds` | `120` | 相同消息去重窗口（秒） |
<!-- schema-defaults:end -->

### 时限与预算如何配合

`gate_context_max_age_seconds=0` 表示不做时间筛选，按判定流水条数取近期消息；正数只允许30–3600秒。机器人上一句、引用和近期图片遵循相同时间开关，近期图片仍对齐已选流水条目。现有会话隔离、内存条数与字符上限保留；不会恢复重载前历史，也不关闭 `gate_reply_max_age_seconds`。

总判定预算与视觉共享预算按调用次数计数，包含复判及兜底。例如文字一次、看图一次、主模型兜底一次，会消耗 3 次总预算和 2 次视觉共享预算。设置 `0` 只关闭该项限制。

文字/视觉时限约束单层，整条判定时限含记忆检索；碎片等待和主回复不属于该判定总时限。自主回复机会有效期从收消息到发送计时，配套开启时仅扣除当前事件有效的从绫准备耗时。模拟打字等待另行处理，新消息与已触发取消仍有效。

### 0的含义

| 设置 | 0表示 |
| --- | --- |
| 上下文有效期 gate_context_max_age_seconds | 关闭时间过滤，条数和字符上限保留 |
| 判定流水条数 gate_context_messages | 不提供近期流水 |
| 碎片等待 fragment_wait_seconds | 不等待碎片，也没有该等待批次的撤回窗口 |
| 最多分几段 max_segments | 不额外限制文本段数，仍遵守宿主发送限制 |
| 每小时判定预算 gate_hourly_budget / gate_vision_hourly_budget | 关闭对应预算限制 |
| 图片观察缓存 gate_image_cache_ttl_seconds | 关闭观察缓存 |
| 生图每日额度 daily_limit | 禁止生图 |

### 模型选择

判定小模型留空跟随会话当前模型；指定项失效可能回退会话模型，应查看日志。视觉候选按已知型号和宿主能力声明筛选，不代表所有 API 均经联网实测。判定可使用已注册路由 provider；上游标签的解析由插件及路由提供者约定，主模型回退列表用逐项下拉添加，按列表顺序回退，不允许重复。

`vision_provider_id` 用于参与判定，`understand_image_provider_id` 用于独立内容理解。原生配置中已移除图片理解桥，旧宿主字段保留但不再由本页编辑。当前事件启用从绫感知时优先沿用其解析，避免重复独立理解。

## AstrBot 原生字段

这些字段读取所选原生配置的实际值，不在本插件 schema 中重新定义默认值。

| 控件键 | AstrBot 字段 | 用途 |
| --- | --- | --- |
| provider_id | agent_runner.config.model.provider_id | 主对话模型；留空按宿主默认 |
| fallback_provider_ids | agent_runner.config.model.fallback_provider_ids | 已注册回退模型顺序 |
| request_max_retries | agent_runner.config.model.request_max_retries | 请求尝试上限，允许 1–10 |
| streaming_response | provider_settings.streaming_response | 原生流式开关 |
| persona_id | agent_runner.config.persona.persona_id | 手动选择并保存后固定；支持无人格 |
| t2i | t2i | 把回复文字排版成图 |
| t2i_word_threshold | t2i_word_threshold | 原生排版字数阈值，允许 0–100000 |
| t2i_strategy | t2i_strategy | local / remote 渲染 |
| t2i_endpoint | t2i_endpoint | 原生远程排版服务地址 |
| t2i_active_template | t2i_active_template | 已配置模板名 |

人格直接显示在原生配置卡片顶部，模型和排版使用可展开二级分组；问号解释保存位置与用途。配置范围收进“编辑范围”，保留既有编辑目标和会话分配规则。主模型改变不连带改变判定小模型。

## AstrBot 原生分段

权威配置为 `platform_settings.segmented_reply`。保留 `enable`、`only_llm_result`、`interval_method`、`interval`、`log_base`、`words_count_threshold`、`split_mode`、`split_words`、`regex` 和 `content_cleanup_rule` 原生字段。

本页“仅对模型结果分段”对应 `only_llm_result` 的模型段落提示，“启用分段回复”对应 `enable` 的多消息发送。细化项 `second_period`、`split_words_enabled`、`max_segments` 编码在原生正则的 `at-guard-reply-v2` 注释中，不另存插件分段配置；默认关闭细化、最大段数 0 不限。旧规则继续兼容，本页保存新控件后进入管理模式。

间隔只由原生管线执行一次；超过 `words_count_threshold` 的原生不分段行为仍保留。第二句号只识别中文 `。`，其他启用分段词立即拆分并重置句号计数。完整示例见[设置分段回复](usage.md#设置分段回复)。

## 独立AI生图

文生图与图生图共用一个分组，不依赖 BiliBot。设置存于旧技术 ID 数据目录的 `image_generation.json`，不复制 API 密钥；连接信息从 AstrBot 已注册模型或路由具体上游读取。API需实际支持所选协议，不能把“支持图片理解”当作“支持生图”。

<!-- image-defaults:start -->
| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `false` | 开启后向本轮主模型提供生图工具 |
| `provider_id` | `""` | 原生模型或路由具体上游，启用前必须选择 |
| `model` | `""` | 可指定同API生图模型；空值沿用所选模型名 |
| `protocol` | `"images"` | 标准 images/generations / images/edits；chat_images 使用 chat/completions 的 image 模态 |
| `daily_limit` | `20` | 全插件按服务器本地自然日共享，0禁止生图，范围0–1000 |
| `size` | `"1024x1024"` | 支持auto、1024x1024、1536x1024、1024x1536 |
| `timeout_seconds` | `90` | 10–180秒；不自动切换上游或重试 |
<!-- image-defaults:end -->

每次请求1张，调用前持久预占额度；失败、超时、取消后的已请求次数不退回。`image_generation_quota.sqlite3` 保存散列请求标识及计数，重载不清零，同消息相同请求不重复生成。生成图片保存在 `generated_images/`，后续生图时清理超过7天的文件。
