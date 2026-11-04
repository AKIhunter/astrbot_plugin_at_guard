# LivingHello 文档

从 [README](../README.md) 了解插件，再按当前任务选择下列入口。本文档描述仓库当前实现；配套插件须满足各自事件约定，不推测任意已部署实例的状态。

| 你要做什么 | 阅读入口 |
| --- | --- |
| 安装后设置、试跑、固定人格、调整分段、查异常 | [使用指南](usage.md) |
| 查默认值、原生字段、保存目标和生效范围 | [配置参考](configuration.md) |
| 看完整判定/生成图、缓存、撤回窗口和配套约定 | [流程与集成说明](architecture.md) |
| 修改代码、执行离线回归、检查发布包或维护记录 | [开发与维护](development.md) |
| 看版本变化 | [更新日志](../CHANGELOG.md) |
| 追溯旧总库迁移 | [历史迁移档案](history/README.md) |

## 权威来源与更新规则

每项内容只保留一个权威位置；README 提供简短介绍并链接详细文档，不复制完整参数表或维护流程。旧专题文档已归并，历史可从 Git 查询；历史迁移档案明确标记为非当前实现基线。

下表的责任人均为仓库维护者或修改对应源文件的贡献者。`Last verified` 表示核对仓库源码的日期；上线状态和付费模型语义质量须另外验证。

| Doc | Diátaxis quadrant | Responsibility path | Source of truth | Last verified | Verification cadence | Staleness signal |
| --- | --- | --- | --- | --- | --- | --- |
| [README](../README.md) | tutorial / 首次入门 | 发布维护者 | metadata、配置及各详细文档 | 2026-11-04 | 每次版本发布 | 版本/名称/入口改变 |
| [使用](usage.md) | how-to | 控制台修改者 | pages/guard、原生桥与发送逻辑 | 2026-11-04 | 每次 UI 或行为变更；90 天复核 | 控件、保存流程或失败原因改变 |
| [配置](configuration.md) | reference | schema / 原生桥修改者 | _conf_schema、guard_native_*、guard_image_settings | 2026-11-04 | 每次字段变更；每个文档 PR 自动校验 | 字段/默认值不匹配或宿主接口变化 |
| [流程](architecture.md) | explanation | 判定/发送/配套修改者 | main、guard_engine、guard_* 及配套源码约定 | 2026-11-04 | 每次链路或配套变更；90 天复核 | 钩子、缓存、撤回或计时约定改变 |
| [维护](development.md) | how-to / operational | 测试与发布维护者 | tests、runner、日志实现、CI、归档属性 | 2026-11-04 | 每次发布；故障后复核 | 命令不可执行、指标/恢复规则改变 |
| [更新日志](../CHANGELOG.md) | reference | 发布维护者 | 对应版本提交与源码 | 2026-11-04 | 每次版本发布 | metadata 新版本没有记录 |
| [历史说明](history/README.md) | reference / archived | 历史档案维护者 | migration-manifest.json | 2026-11-04 | 档案路径或解释变更时 | 被引用为当前源码哈希 |

超过复核周期或发生对应变更而未更新时，标为 `stale` 并核对来源；不能继续当作当前操作依据。源码修改和文档更新进入同一提交或关联 PR。CI 在每个 PR / 主分支推送检查 Markdown 链接、配置表、版本、资产和离线回归。模型、阈值、依赖或线上故障改变时，同步检查使用排查步骤和维护指标解释。

## 旧入口归并

| 原文件 | 当前入口 |
| --- | --- |
| current-features.md | [README](../README.md) 与[配置参考](configuration.md) |
| compact-config-help.md、reply-delivery.md | [使用指南](usage.md) |
| native-settings-flow.md | [配置参考](configuration.md) 与[流程说明](architecture.md) |
| fragments.md、reply-topic-flow.md、expression-takeover.md、congling-compat.md | [流程说明](architecture.md)；操作见[使用指南](usage.md) |
| console-records.md、maintenance.md | [开发与维护](development.md) |
| migration-manifest.json | [history/migration-manifest.json](history/migration-manifest.json) |
