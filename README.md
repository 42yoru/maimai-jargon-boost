# 黑话加速器（Jargon Boost）

<p>
  <a href="https://github.com/42yoru/maimai-jargon-boost/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-jargon-boost/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.15%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.16.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python">
</p>

> [!NOTE]
> **这个插件是给消息量不大的麦麦准备的。** 群聊不多、聊得不密集时，黑话功能很容易「查无此人」——词条攒不到推断阈值，
> 永远停在未确认。插件不改 Maibot 本体，只是把够格的记录提前推到阈值，让黑话功能重新有存在感。

让**已经出现够多次、但还没到主程序推断阈值**的黑话提前进入确认流程。

> **先回答三个常见问题**
>
> - **它解决什么？** 主程序要等一条黑话累计命中 4 次才会跑第一次含义推断，但很多词在群里只出现一两次就沉底了，「已确认黑话」长期不涨。插件把够格的记录 `count` 提前补到首推档，让主程序自己的推断流程把它确认下来。
> - **会替我生成含义吗？** 不会。插件只改一个整数 `count`；三段推断、prompt、含义写回、`is_jargon` 判定全部沿用主程序。
> - **要改主程序吗？** 不用。纯插件，只调用官方 hook `jargon.extract.before_persist` 和已注册的 `database.query` / `send.text` 能力。

## 先看效果

每次黑话提取写库前，日志里会出现：

```
黑话加速器：「楠娘」count 2 → 3（下一次提取将触发主程序推断）
```

下一次裁切学习命中它时，主程序会照常完成确认：

```
[示例群]楠娘的含义是 “楠娘”是网络用语/黑话，基本等同于“男娘”……
```

不想等裁切时，直接用命令：

```
/黑话加速 dry
→ 黑话加速器（预演）：候选 20 条，处理 20 条，失败 0 条。
  - 「示例词」count 2 → 3
  …
```

## 三个必须知道的点

### 1. 黑话学习只在上下文裁切时跑

主程序只在**上下文的真实收发消息数**达到 `ceil(max_context_size × 1.2)` 时裁切，并提交后台学习。下面是 Maibot 源码的默认值（`src/config/official_configs.py` 的 `ChatConfig`），实际以 `bot_config.toml` 的 `chat.max_context_size` / `chat.max_private_context_size` 为准：

| 聊天类型 | 配置项 | 源码默认 | 裁切阈值 | 裁切后保留 |
|---|---|---|---|---|
| 群聊 | `chat.max_context_size` | 30 | 36 条 | 6 条 |
| 私聊 | `chat.max_private_context_size` | 60 | 72 条 | 12 条 |

阈值和保留条数由 `src/maisaka/context/post_processor.py` 计算：`TRIM_THRESHOLD_RATIO = 1.2`、`TRIM_TARGET_RATIO = 0.2`、`MIN_RETAINED_CHAT_MESSAGES = 5`。你的配置调高或调低，阈值会跟着变；消息量小的场景到不了阈值，这正是提供手动入口的原因。

### 2. 只补「首推」，不重复补

同时满足才会补：

- 记录属于当前会话语义范围（`shared_globally=true` 时全部放行；否则只认当前会话和 `is_global` 记录）；
- `created_by` 不是 `MANUAL`（手动录入的记录本来就不推断）；
- `is_jargon=False`、`is_complete=False`；
- `last_inference_count == 0`（只处理第一次推断，后续 8 / 25 / 100 档交给真实 count）；
- `min_count <= count < target_count`。

补完 `count` 到 3，主程序下次提取命中它时 +1 到 4，正好触发首推；之后的档位仍按真实命中次数走。

### 3. `count` 会虚高 1~2 次

只影响统计展示，不影响推断正确性。停用插件后已补高的数值不会自动回滚。

## 五分钟上手

### 第 1 步：装插件

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai-jargon-boost.git
```

仓库里**不带** `config.toml`（它被 `.gitignore` 挡住）。首次加载时宿主会按配置模型自动生成一份，想改就改它，或者去 WebUI 的插件配置页点。

### 第 2 步：确认共享组

插件默认 `shared_globally = false`，只处理**当前会话**的记录，这是最保守的默认值。

如果你的 `bot_config.toml` 里是 `*/*` 全局共享：

```toml
[[jargon.jargon_groups]]
[[jargon.jargon_groups.targets]]
platform = "*"
item_id = "*"
```

就把 `shared_globally` 改成 `true`，插件会和主程序一样处理所有会话的记录。

> [!WARNING]
> `shared_globally = true` 时插件会对**所有** `Jargon` 记录放行，包括不属于当前黑话共享组的会话。确认是 `*/*` 全局共享再打开；只共享部分群时保持 `false`。开启后插件加载时会打印一条 warning 提醒核对。

### 第 3 步：验证

在群里发一句带来黑话的闲聊，或者直接用手动入口扫一遍。日志出现「黑话加速器」就说明钩子通了。

## 配置

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

| 配置项 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 插件总开关 |
| `boost.enabled` | `true` | 提前确认开关；关掉后钩子仍在，但不会补 count |
| `boost.min_count` | `2` | 词条至少被抽到几次才有资格补 |
| `boost.target_count` | `3` | 补到的 `count`；主程序随后再 +1，所以 3 表示刚好到首推阈值 4 |
| `boost.max_boosts_per_batch` | `2` | 每次提取最多补几条，控制同时触发的推断数量（每条推断 3 次 LLM 调用） |
| `boost.shared_globally` | `false` | 默认只处理当前会话；`bot_config.toml` 是 `*/*` 全局共享时才改成 `true`，否则会给别的群的词条补 count |
| `boost.dry_run` | `false` | 只写日志、不改 `count` |

WebUI 配置页按「常用设置 / 高级」两个页签展示，带中文标签和提示。

## 手动入口

| 入口 | 用法 | 说明 |
|---|---|---|
| 命令 | `/黑话加速` | 扫描并补 count，最多 20 条（`permission="operator"`，由宿主真实校验） |
| 命令 | `/黑话加速 dry` | 只预演，返回会补哪些词，不写库 |

**为什么只有命令、没有工具**：宿主的 `permission` 元数据只被命令链路消费
（`_build_command_info` → `CommandInfo` → `has_command_permission`），工具的
`_build_tool_definition` 根本不读它。也就是说给工具声明 `permission="operator"` 只是装饰，
任何会话都能通过模型调用它写库。所以这个插件**不提供写库工具**，手动入口只有 operator 命令。

## 设计说明

### 为什么不自己推断

推断涉及三段 LLM 调用、含义比较、`last_inference_count` 写回和 Hook 链。插件复制一份逻辑，就会和主程序出现两套判定、互相打架。补 `count` 只是「让主程序的阈值条件提前成立」，其余全部复用主程序，升级和改 prompt 都只需要改一处。

### 异常关闭 / 重启

- 插件不保存任何内存状态，判断全部读数据库当前值；进程被强杀、bot 异常退出后重启，行为一致。
- 补 `count` 的条件是 `count < target_count`，补完就不满足，天然幂等，不会重复补。
- 补 `count` 是单条 UPDATE，SQLite 事务保证要么生效要么没发生：补完崩了，下次提取主程序会读到新值照样推断；没补成功，下次提取重新补。
- 插件内部用 `asyncio.Lock` 串行化数据库读写，多聊天流同时提取、手动命令并发都不会重复补同一条。
- 钩子任何异常只记日志并放行，不会打断主程序的黑话提取。

### 能力声明

宿主的权限令牌按名字精确授权，`_manifest.json` 的 `capabilities` 少声明一项就会返回 `E_CAPABILITY_DENIED`。本插件只声明 `database.query`（查/改 `Jargon` 记录）和 `send.text`（命令回执）。

## 开发与测试

```bash
git clone https://github.com/42yoru/maimai-jargon-boost.git
cd maimai-jargon-boost
pip install "maibot-plugin-sdk>=2.16.0" ruff
ruff check . --select E,F,W,B --line-length 130
python -m unittest discover -s tests -t . -v
```

GitHub Actions 会在 3.11 / 3.12 / 3.13 上跑同一套检查。

## License

MIT
