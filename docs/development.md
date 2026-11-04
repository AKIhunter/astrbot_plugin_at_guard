# 开发与维护

面向代码贡献者及维护者：从完整克隆执行回归、维护日志并准备发布。权威来源是仓库源码、[tests](../tests/)、[离线入口](../run_offline_tests.py) 和 [CI](../.github/workflows/checks.yml)。已核对源码：2026-11-04；每次发布、宿主接口变更或故障后重新核对。本页不作为任意服务器的已部署状态证明。

## 仓库布局

```text
README.md                    首页与首次使用
CHANGELOG.md                 版本变化
metadata.yaml                商店元数据与兼容版本
logo.png                     256×256 图标
main.py / guard_*.py          运行实现，保持既有导入路径
_conf_schema.json            插件参数的权威默认值
pages/guard/index.html       四页控制台
docs/                       使用、配置、流程、维护、历史档案
tests/test_*.py              独立回归与可选配套测试
scripts/check_docs.py        标准库文档校验
run_offline_tests.py         根目录兼容运行入口
.github/img/Banner.png       README Banner
.github/workflows/checks.yml 文档和离线回归
```

正式仓库为 `AKIhunter/astrbot_plugin_livinghello`，默认分支 `main`。独立维护根目录源码，不再从旧总库同步或执行 subtree split。技术 ID 保留 `astrbot_plugin_at_guard`，不要为了名称统一而搬数据、改 API 或重命名运行模块。

## 从完整克隆执行验证

需要 Python 3.10 及以上，以实际宿主支持的版本为准：

```bash
git clone https://github.com/AKIhunter/astrbot_plugin_livinghello
cd astrbot_plugin_livinghello
python3 -B scripts/check_docs.py
python3 -B run_offline_tests.py
```

以上为使用者在自己终端执行的步骤。`run_offline_tests.py` 保留旧运行入口，自动发现 `tests/test_*.py`。没有 AstrBot 时安装最小框架替身，运行真实插件方法，不接触部署、付费模型或真实平台。已安装宿主时可用其解释器；不要在已连接真实平台的运行进程中加载测试。

宿主与可选配套测试需要源码位置：

```bash
ASTRBOT_NATIVE_SOURCE=/path/to/astrbot \
CONGLING_HANDOFF_SOURCE=/path/to/astrbot_plugin_congling \
LIVINGMEMORY_TIMING_SOURCE=/path/to/LivingMemory/core/retrieval/request_timing.py \
python3 -B run_offline_tests.py
```

`ASTRBOT_NATIVE_SOURCE` 指向含 `core/` 的 AstrBot 包目录。未提供源码时相关集成测试标记跳过，独立回归继续；报告应区分通过和跳过，不能将独立通过当作配套验收。标准库 unittest 也可在设置 `PYTHONPATH` 后从 `tests/` 发现，但根入口更方便。

回归覆盖语义否决、视觉/记忆降级、碎片合并、撤回窗口、结果交付、分段、保存冲突、人格固定、记录恢复及配套交接。调整模型或提示词后仍需用脱敏实际案例验证语义；离线通过不证明真实准确率或线上 Token 节省比例。

## 文档检查与发布

`scripts/check_docs.py` 使用标准库检查 Markdown 本地路径与锚点、完整 schema 默认值表、独立生图默认值和原生字段保存路径、当前版本、图标尺寸及测试布局。源码常量通过AST读取，不导入或启动插件。每个 PR 及主分支推送执行文档检查和独立离线回归，不需要密钥。

发布前还应使用可用 YAML 解析器解析 metadata，确认 GitHub Markdown 与 Mermaid 渲染。版本以 metadata 为准，同步控制台版本和更新日志；源代码若增加注册版本也须保持一致。

`.gitattributes` 在安装归档中排除 `tests/`、`docs/`、开发脚本和 CI；首页、CHANGELOG、logo、Banner 与运行代码保留。开发请完整克隆，安装包不包含完整文档与回归模块：

```bash
git diff --check
git archive --format=zip HEAD -o /tmp/livinghello-release.zip
```

确认包小于 16 MiB，不含缓存、依赖目录、用户配置、日志、数据库、密钥或备份。提交精确文件，中文说明目的；fetch 后检查是否分叉，发现冲突停止，不强推。新版本上线前备份插件与相关配置，重载后检查插件加载和服务健康；商店审核属于单独发布步骤。

## 日志与指标

默认数据目录沿用 `data/plugin_data/astrbot_plugin_at_guard/`：

| 文件或指标 | 权威含义 | 更新/复核触发 |
| --- | --- | --- |
| decisions.jsonl | 判定上下文、结论、模型原话与能力耗时 | 日志结构或采集逻辑变化；故障复核 |
| gate_state.json | 今日计数，按自然日切换 | 日期切换、计数逻辑或恢复问题 |
| participation_stats.json | 累计参与次数、分布等 | 判定口径变更后核对解释；不自动清零 |
| native_persona_pins.json | 各原生配置的固定人格 | 人格修改、删除、损坏或重载 |
| image_generation.json | 独立生图公开配置，不含连接密钥 | 生图设置保存、模型变更 |
| image_generation_quota.sqlite3 | 每日共享额度与散列请求状态 | 请求预占、完成或结果未知；重载不清零 |
| Decision.timings | 参与判定阶段能力等待，含失败 | 能力、并发、配套采集接口变化 |

`DecisionLog` 在 `decisions.jsonl` 超过 1 MiB 时轮转到时间戳归档，不覆盖已有归档；兼容旧 `.1`。启动倒序扫描多个文件，分块读取恢复最近 600 条有效 JSON，忽略坏行。默认/搜索最多返回 100 条，不承诺全历史搜索。退出或热重载强制保存当日计数，避免写盘节流遗漏最后计数。

新记录含完整会话类型、群 ID 或私聊对象；旧私聊从会话标识恢复，群发送者不当作私聊对象。正文在页面按文本输出。普通保存不清零；独立清零操作须确认并保留日志。

能力指标的每项包含 `ms`、`calls`、`status`：

| 项 | 测量内容 |
| --- | --- |
| text | 所有文字判定，含记忆复判 |
| vision / main_fallback | 视觉/主模型判定调用，含异常和超时 |
| memory | 身份解析、检索及结果复核总等待 |
| embedding | 请求向量或等待共享向量任务 |
| vector_index / memory_read | 原生向量搜索、文档及可见候选读取 |
| vector_adapter | 旧宿主无法拆分的向量检索总等待 |
| retrieval_* | LivingMemory 各并发检索分支等待 |

总等待包含子步骤，并发分支可能等同一个任务，**不能相加当作总耗时**。指标不包含碎片等待、从绫爬楼和主回复生成。未调用显示“未调用”，旧记录未采集显示“未采集”，不冒充 0 ms。失败和取消也在 finally 中采集。

LivingMemory 的 `core/retrieval/request_timing.py` 可通过请求局部 ContextVar 传指标；并发隔离、嵌套恢复，不改变身份、查询或隐私边界。配套没有采集器时不要猜细分时间。

## 维护与恢复

1. 修改前备份插件、目标配置和数据目录，记录当前提交；保留用户已保存值。
2. 本地或隔离环境验证失败分支、原生保存及配套接口，避免将本地旧文件整体覆盖线上权限补丁。
3. 重载后检查插件加载错误、目标配置、人格和测试会话发送效果；确认服务正常。文档/测试整理无需重载运行服务。
4. 异常先看实际判定原因、上下文版本和保存结果，再选择回退；不要自动重放未知发送结果。
5. 回退代码时恢复备份并重载；只恢复本次影响范围，不覆盖运行期间产生的聊天、记忆或已读状态。关闭准备计时适配即可恢复原年龄算法。

日志包含聊天内容，备份和支持材料限制访问并脱敏。服务器 LivingMemory 可能包含额外权限筛选补丁，增加计时时保留 `adapter_sql`、`adapter_visible`、FAISS IDSelector 等过滤逻辑；计时不能改变隐私边界。

历史迁移原哈希仅用于追溯：[迁移档案](history/README.md)。不要用旧快照覆盖之后新增的修复或运行数据。

## WebUI 接口索引

路由前缀沿用 `astrbot_plugin_at_guard`，通过 AstrBot 插件页面桥接和权限机制访问。

| 路径 | 方法 | 用途 |
| --- | --- | --- |
| console/state | GET | 状态、参数与最近记录 |
| console/decisions | GET | query / 分类搜索近期记录 |
| console/config | POST | 参数白名单保存 |
| console/native-segments | GET / POST | 原生分段读取 / 版本校验保存 |
| console/native-settings | GET / POST | 原生模型与固定人格 |
| console/image-settings | GET / POST | 独立AI生图设置、模型/路由候选和每日额度 |
| console/flow | GET | 判定及生成流程结构 |
| console/probe | POST | 文字判定试跑，有 API 用量 |
| console/reset | POST | 明确清零统计，保留日志 |

实现见 [guard_webui.py](../guard_webui.py)。POST 通过宿主桥接请求与权限检查，不将内部路由示例解释为无需认证的公开服务。
