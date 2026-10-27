# 更新日志

## 0.2.0

- 新增工具 `jargon_boost_run` 和命令 `/黑话加速`：可以手动扫描并补 count，`/黑话加速 dry` 只预演不写入。
- 新增 `shared_globally` 配置：与主程序的黑话共享组（`*/*`）保持一致；关闭后插件只处理当前会话的记录。
- 新增 `dry_run` 配置：只写日志、不改数据，方便观察会补哪些词。
- 新增 WebUI 中文配置页（页签 + 中文标签 + 提示），只重组展示结构，不改配置模型。
- 补齐 `on_load` / `on_unload` / `on_config_update` 生命周期回调（宿主要求）。
- 加载时自检 `database.query` 能力回环，提前发现能力授权或 RPC 问题。
- 补充单元测试（`tests/test_jargon_boost.py`）。

## 0.1.0

- 首个版本：在 `jargon.extract.before_persist` 钩子里，把「已经抽到够多次、但从未推断过」的黑话记录 count 补到首推阈值，让主程序自己触发三段推断。
