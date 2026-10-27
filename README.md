# 黑话提前确认（jargon_boost）

不改主程序的小补丁：让「已经出现够多次、但还没到主程序推断阈值」的黑话尽快被确认。

## 为什么需要它

主程序的推断阈值写死在 `src/learners/jargon_miner.py` 的 `JARGON_INFERENCE_THRESHOLDS = [4, 8, 25, 100]`。
一条黑话要累计命中 4 次才会跑第一次含义推断，而很多词在群里只出现一两次就沉底了，
于是「已确认黑话」长期不增长。

本插件调用官方 Hook `jargon.extract.before_persist`，在每次提取写库前，
把满足条件的记录 `count` 提前补到首推阈值。随后主程序会：

1. 把 count 再 +1；
2. 用 `_should_infer_meaning` 判定进入推断；
3. 用原有的三段推断、prompt 和含义写回流程完成确认。

插件不生成含义、不改判定逻辑、不写 `is_jargon`，只推一把 count。

## 补 count 的条件

同时满足才会补：

- 记录属于当前会话（`session_id_dict` 含当前会话）或 `is_global=True`；
- `created_by` 不是 `MANUAL`（手动记录本来就不推断）；
- `is_jargon=False`、`is_complete=False`；
- `last_inference_count == 0`（只处理「首推」，后续档位交给真实 count）；
- `min_count <= count < target_count`。

## 配置

```toml
[plugin]
enabled = true

[boost]
enabled = true
min_count = 2              # 至少被抽到 2 次才补
target_count = 3           # 补到 3；主程序再 +1 刚好到首推阈值 4
max_boosts_per_batch = 2   # 每次提取最多补 2 条，控制同时推断的数量
```

## 异常关闭 / 重启行为

- 插件不保存任何内存状态，判断全部基于数据库当前值；进程被强杀、bot 异常退出后重启，行为一致。
- 补 count 条件是 `count < target_count`，补完就不满足，天然幂等，不会重复补。
- 补 count 是单条 UPDATE，SQLite 事务保证要么生效要么没发生：
  - 补成功但主程序来不及更新就崩溃 → 下次提取时主程序读到新 count，仍会触发推断；
  - 补失败 / 超时 → hook 只记日志并放行，下次提取重新判断。
- 插件内部串行化数据库读写（`asyncio.Lock`），多聊天流同时提取不会重复补同一条记录。

## 副作用与回滚

- `count` 会被人为抬高 1~2 次，只影响统计展示，不影响推断正确性。
- 禁用插件（`[plugin] enabled = false`）即停止补 count，已抬高的数值不会自动回滚。
- 想彻底移除：停用插件后删除本目录即可，主程序没有任何改动。
