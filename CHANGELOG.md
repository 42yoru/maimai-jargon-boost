# 更新记录

## 0.2.2

**修复**

- `/黑话加速` 命令补上 `permission="operator"`：之前默认对所有人生效，任何群成员发一句就能写库并触发一批 LLM 推断；
  现在只有操作员能执行，自动钩子路径不受影响

**细节**

- manifest 的 `plugin_type` 改为 `data`（插件推动的是黑话记忆数据的推断节奏，分类更准确）
- `shared_globally=true` 时加载日志会打印一条 warning，提醒核对黑话共享组配置；README 同步加了风险提示
- 单元测试补一条命令权限断言

## 0.2.1

**功能**

- 插件更名为「黑话加速器」：原名「黑话提前确认」不容易一眼看懂，新名字与命令 `/黑话加速`、英文名 Jargon Boost 对齐
- 日志、命令回执、WebUI 配置页标题同步改名；插件 ID、仓库目录、命令名保持不变

**细节**

- 配置页「提前确认」分区更名为「加速参数」

## 0.2.0

**功能**

- 新增工具 `jargon_boost_run` 与命令 `/黑话加速`：可以手动扫描并补 count，`/黑话加速 dry` 只预演不写入，
  不用等上下文裁切
- 新增 `shared_globally` 配置：与主程序的黑话共享组（`*/*`）保持一致；关闭后插件只处理当前会话的记录
- 新增 `dry_run` 配置：只写日志、不改数据，方便观察会补哪些词
- 新增 WebUI 中文配置页（页签 + 中文标签 + 提示），只重组展示结构，不改配置模型和 TOML 路径
- 新增单元测试（20 项）与 GitHub Actions CI（3.11 / 3.12 / 3.13）

**细节**

- 插件 ID 改为 `github.42yoru.maimai-jargon-boost`，目录名与仓库名统一为 `maimai-jargon-boost`
- 补齐 `on_load` / `on_unload` / `on_config_update` 生命周期回调（宿主要求，缺一个就无法加载）
- 加载时做一次只读自检（`database.query`），提前发现能力授权或 RPC 回环问题
- manifest：图标改为本地 `icon.png`（fallback `zap`），补 `plugin_type`、`changelog`，
  版本区间收到 `host 1.3.15 ~ 1.3.99`、`sdk 2.16.0 ~ 2.16.99`
- `config.toml` 不再进仓库（由宿主按配置模型生成），另提供 `config.toml.example`
- `.gitignore` / `.gitattributes` / CI 与其他 42yoru 插件对齐
- README 顶部补「适用场景」说明：写给消息量不大的麦麦，明确不改主程序；示例日志里的真实群名
  和词条换成通用占位
- 图标换成插画版

## 0.1.0

**功能**

- 首个版本：在 `jargon.extract.before_persist` 钩子里，把「已经抽到够多次、但从未推断过」的黑话记录
  count 补到首推阈值，让主程序自己触发三段推断
