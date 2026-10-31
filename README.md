# 黑话加速器（Jargon Boost）

<p>
  <a href="https://github.com/42yoru/maimai-jargon-boost/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-jargon-boost/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.15%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.12%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python">
</p>

> **一句话定位：** 把已经出现足够次数、但尚未达到主程序首轮推断阈值的黑话记录提前补到首推档，让主程序完成后续确认。

## 适用与不适用

**适用：**

- 群聊较少、黑话记录长期达不到主程序第一次推断阈值；
- 希望复用主程序自己的三段推断、prompt、含义写回和 `is_jargon` 判定；
- 需要手动扫描或在 `jargon.extract.before_persist` hook 中自动加速。

**不适用：**

- 不生成黑话含义，不替代主程序推断；
- 不提供模型可调用的写库工具；手动写库入口只有宿主实际校验 `permission="operator"` 的命令；
- 不会自动回滚已经补高的 `count`。补高只影响统计展示，停用插件不会撤销历史修改。

## 快速安装

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai-jargon-boost.git
```

仓库不包含 `config.toml`；首次加载时宿主会按配置模型生成。也可以直接使用 WebUI 配置页。

## 最小配置

默认配置已经可以在当前会话范围内工作：

```toml
[plugin]
enabled = true

[boost]
enabled = true
min_count = 2
target_count = 3
max_boosts_per_batch = 2
shared_globally = false
dry_run = false
```

如果主程序的黑话共享组确实是全局 `*/*`，才将 `shared_globally` 改为 `true`：

```toml
[[jargon.jargon_groups]]
[[jargon.jargon_groups.targets]]
platform = "*"
item_id = "*"
```

## 工作流程

1. 在 `jargon.extract.before_persist` 中读取主程序即将处理的黑话条目。
2. 查询 `Jargon` 记录，按会话范围、来源、完成状态、推断次数和 `count` 条件筛选。
3. 对符合条件的记录，将 `count` 补到 `target_count`；插件不改含义、不执行 LLM 推断。
4. 主程序下次提取命中该记录时继续按自己的规则递增，并在达到阈值后完成首轮推断。
5. 每次批处理最多处理 `max_boosts_per_batch` 条；插件内部用锁串行化数据库读写。

默认首推逻辑是：记录 `count` 在 `min_count`（含）到 `target_count`（不含）之间，`is_jargon=false`、`is_complete=false`、`last_inference_count=0`，且不是 `MANUAL` 记录。补到 3 后，主程序下一次命中加 1 到 4，触发第一次推断。

## 配置表

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 插件总开关 |
| `boost.enabled` | `true` | 是否执行 count 加速 |
| `boost.min_count` | `2` | 进入候选所需的最小 count |
| `boost.target_count` | `3` | 补到的 count；主程序随后再加 1 |
| `boost.max_boosts_per_batch` | `2` | 每次 hook 最多补几条 |
| `boost.shared_globally` | `false` | 是否跨会话处理；仅在共享组为 `*/*` 时启用 |
| `boost.dry_run` | `false` | 只记录候选、不写入数据库 |

手动命令：

| 命令 | 作用 |
| --- | --- |
| `/黑话加速` | 扫描并补 count，默认最多处理 20 条，命令权限由宿主校验 |
| `/黑话加速 dry` | 预演候选，不写数据库 |

## 边界与隐私

- 插件只声明并使用 `database.query` 与 `send.text` 能力。
- `shared_globally=false` 时只处理当前会话和主程序标记为全局的记录；启用全局模式会放行所有 `Jargon` 记录，请先确认主程序共享组配置。
- `count` 是人为提前补高的统计值，不代表真实出现次数；停用插件不会自动回滚。
- 当前宿主的 `database.query` 可能把数据库异常折叠为空列表；插件能识别明确错误或错误形状，但空列表本身不能证明数据库健康。若预期有候选却返回空，请同时查看宿主数据库日志。
- hook 异常只记录日志并放行，不应阻断主程序黑话提取。

## 测试

在插件目录运行：

```bash
python -m pytest tests -q
```

CI 还会在 Python 3.11、3.12、3.13 上运行检查。测试覆盖候选筛选、会话范围、同名记录、手动扫描、配置和异常路径。

## 插件信息

| 项目 | 值 |
| --- | --- |
| 插件 ID | `github.42yoru.maimai-jargon-boost` |
| 版本 | `0.4.0` |
| 仓库 | <https://github.com/42yoru/maimai-jargon-boost> |
| 许可证 | MIT |
