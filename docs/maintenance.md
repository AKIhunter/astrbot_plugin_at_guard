# at_guard 独立维护

维护仓库：https://github.com/AKIhunter/astrbot_plugin_at_guard

2026-11-02 起，本仓库直接维护 at_guard 源码，默认分支为 `main`。
原来的市场发布镜像已经成为维护仓库；插件 ID、版本、安装地址与提交历史保持原位。

修改根目录代码、配置 schema 和 `pages/guard/` 的控制台，验证后在本仓库提交、推送。
无需从 `astrbot_plugin` 总库同步或运行 `git subtree split`。
使用场景、限制和设置步骤都在 `README.md`，它也是仓库首页的使用入口。

安装档案排除 `test_*.py` 与 `docs/`；开发时完整克隆仓库，再使用装有 AstrBot
的解释器运行 `python -m unittest discover -s . -p 'test*.py'`。
合成回归测试通过不能证明真实群聊的语义准确率；调整判定模型或提示词时仍需检查实际案例。

不要提交 `data/`、判定日志、历史聊天评估文件、用户配置或凭据。
`.gitignore` 保留相应保护规则。核心运行代码与迁移前版本逐文件一致，
`docs/migration-manifest.json` 记录旧总库提交及原哈希。

原市场条目：
https://cloud.astrbot.app/plugin/AKIhunter/astrbot_plugin_at_guard
本次维护结构变化不重复提交相同版本审核，也不修改服务器运行配置。

旧总库保留历史并归档；必要时仍可读取旧快照或撤销归档。
日常维护以本仓库为准，避免恢复旧快照时覆盖之后新增的修复和运行数据。
