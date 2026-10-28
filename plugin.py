"""黑话加速器插件（jargon_boost）。

主程序的黑话含义推断要等某条黑话的 count 涨到 4 / 8 / 25 / 100 才触发，
但很多词条在聊天里只被抽到一两次就沉底了，于是长期停在「未确认」。

本插件不改主程序，只在每次黑话提取（`jargon.extract.before_persist`）写库前，
把「已经有足够证据、但从未推断过」的记录 count 提前补到首推阈值：
主程序随后会自己把 count 再 +1，并通过 `_should_infer_meaning` 判定，
用原有的三段推断、原有的 prompt 和原有的含义写回流程完成确认。

也提供手动入口（工具 `jargon_boost_run` / 命令 `/黑话加速`），
不想等提取时可以直接扫一遍库、把够格的记录补上。

为什么这样设计（异常关闭 / 重启相关）：

1. 插件不保存任何「已处理」内存状态，全部判断都读数据库当前值。
   无论插件还是 bot 在哪个环节被强杀，重启后行为完全一致。
2. 判断条件是 count < target_count 且 last_inference_count == 0，
   补完后 count 已到阈值，条件不再成立，天然幂等，重启不会重复补。
3. 补 count 只是一条 UPDATE，SQLite 事务保证要么生效要么没发生。
   如果补完、主程序还没来得及更新就崩溃，下次提取时主程序会读到补过的
   count 并触发推断；如果没补成功，下次提取会重新补。不会漏也不会重复。
4. 主程序自己的推断、写入、Hook 链全部保持不变，插件只在最前面推一把。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import asyncio
import json

from pydantic import model_validator

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config_ui import build_config_ui_schema

JARGON_MODEL_NAME = "Jargon"

JARGON_SCAN_WINDOW = 200
"""钩子路径一次取回多少条「还没推断过」的记录用于内存匹配；没覆盖到的会再做精确查询。"""

JARGON_FIRST_INFERENCE_THRESHOLD = 4
"""宿主首次推断的 count 阈值：把 count 补到 3，主程序本轮再 +1 就正好落到这里。"""
"""主程序 Jargon 数据模型名，对应 database_model 中的类名。"""

MANUAL_CREATOR = "MANUAL"
"""手动录入来源；这类记录主程序不会推断，插件也不应干预。"""

DEFAULT_MANUAL_LIMIT = 20
"""手动入口默认最多补多少条。"""

MAX_MANUAL_LIMIT = 100
"""手动入口单次上限，避免一次请求扫太多记录。"""

MAX_BOOST_LOGS = 10
"""手动入口回执里最多列多少条明细。"""


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class BoostSectionConfig(PluginConfigBase):
    """黑话加速器参数。"""

    __ui_label__ = "加速参数"
    __ui_icon__ = "zap"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否启用提前确认（插件总开关打开时才生效）",
    )
    min_count: int = Field(
        default=2,
        ge=1,
        description="词条至少被抽到几次才补 count；设为 1 表示首次出现就推断，证据会偏少",
    )
    target_count: int = Field(
        default=3,
        ge=1,
        le=JARGON_FIRST_INFERENCE_THRESHOLD - 1,
        description=(
            "补到的 count 值；主程序随后会再 +1，所以 3 表示刚好落到首推阈值 4。"
            f"上限 {JARGON_FIRST_INFERENCE_THRESHOLD - 1}：再高就会一次跨过后续档位，"
            "甚至把记录直接标成已确认"
        ),
    )

    @model_validator(mode="after")
    def _check_count_order(self) -> "BoostSectionConfig":
        """min_count 必须小于 target_count，否则一条词条都补不到（插件会静默失效）。"""

        if self.min_count >= self.target_count:
            raise ValueError(
                f"min_count({self.min_count}) 必须小于 target_count({self.target_count})，"
                "否则没有任何词条能被提前确认"
            )
        return self
    max_boosts_per_batch: int = Field(
        default=2,
        ge=1,
        le=10,
        description="每次提取最多补几条，控制同时触发的推断数量（每次推断会调用 3 次 LLM）",
    )
    shared_globally: bool = Field(
        default=False,
        description=(
            "默认只处理当前会话的记录；只有 bot 的黑话共享组是 */* 全局共享时才改成开启，"
            "避免给别的群的词条补 count"
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="只写日志、不修改 count，用来观察会补哪些词",
    )


class JargonBoostConfig(PluginConfigBase):
    """黑话加速器插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    boost: BoostSectionConfig = Field(default_factory=BoostSectionConfig)


class JargonBoostPlugin(MaiBotPlugin):
    """在每次黑话提取写库前，把够格的记录 count 提前补到首推阈值。"""

    config_model = JargonBoostConfig

    @classmethod
    def build_config_schema(cls, **plugin_info: Any) -> Dict[str, Any]:
        """只重组 WebUI 的展示结构，配置模型与 TOML 路径保持不变。"""

        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    def __init__(self) -> None:
        super().__init__()
        # 多个聊天流可能同时触发提取，手动命令也可能并发调用；串行化数据库读写，
        # 避免同一条记录被并发补两次。
        self._lock = asyncio.Lock()

    # ── 生命周期 ──────────────────────────────────────────────────

    async def on_load(self) -> None:
        """插件加载完成后输出一次生效参数，并自检插件到宿主的数据库能力回环。"""

        config = self.config.boost
        self.ctx.logger.info(
            "黑话加速器已加载：min_count=%s target_count=%s max_boosts_per_batch=%s shared_globally=%s dry_run=%s",
            config.min_count,
            config.target_count,
            config.max_boosts_per_batch,
            config.shared_globally,
            config.dry_run,
        )
        # 只读自检：确认钩子里要用的 database.query 能力能通，免得等到真正提取时才发现回环问题。
        # 形状不对一律抛：db_get 内部会把异常吞成空列表，插件再"当没查到"就永远发现不了故障。
        rows = await self.ctx.db.query(JARGON_MODEL_NAME, query_type="get", limit=1)
        if isinstance(rows, dict):
            raise RuntimeError(f"数据库回环自检失败，database.query 返回错误: {rows.get('error') or rows!r}")
        if not isinstance(rows, list):
            raise TypeError(f"数据库回环自检失败，database.query 返回形状异常: {type(rows).__name__}")
        self.ctx.logger.info("数据库回环自检通过：database.query 可用（样例记录 %s 条）", len(rows))
        if config.shared_globally:
            # shared_globally=true 时插件对库里所有 Jargon 记录放行；只有确认 bot 的黑话共享组是 */* 才安全，
            # 但插件读不到那份配置，只能提醒用户自己核对。
            self.ctx.logger.warning(
                "黑话加速器：shared_globally=true 会对所有 Jargon 记录补 count。"
                "如果你的 bot 只共享部分群，请把它改成 false，否则别的群的词条也会被提前确认。"
            )

    async def on_unload(self) -> None:
        """插件卸载回调；本插件不持有需要落盘的状态。"""

        self.ctx.logger.info("黑话加速器已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新回调；插件没有缓存，读取时始终以 self.config 为准。"""

        del config_data, version
        self.ctx.logger.info("黑话加速器配置已更新：scope=%s", scope)

    # ── 钩子 ─────────────────────────────────────────────────────

    @HookHandler(
        "jargon.extract.before_persist",
        name="jargon_boost_raise_count",
        description="把够格的黑话记录 count 补到首推阈值，让主程序自己触发含义推断",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=4000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def before_persist(self, **kwargs: Any) -> Dict[str, Any]:
        """黑话提取写库前的钩子入口。

        Args:
            **kwargs: 主程序传入的 hook 参数，包含 session_id、session_name、entries。

        Returns:
            Dict[str, Any]: 始终放行的 hook 结果。
        """

        session_id = str(kwargs.get("session_id") or "")
        entries = kwargs.get("entries")
        # 这里不做 try/except：宿主 runner 会 logger.error(..., exc_info=True) 记完整堆栈，
        # 再按本插件声明的 error_policy=SKIP 继续黑话提取。插件自己再包一层只会把 traceback 换成一行字符串。
        if session_id and isinstance(entries, list) and self._is_active():
            await self._boost_entries(session_id, entries)
        return {"action": "continue"}

    # ── 手动入口 ─────────────────────────────────────────────────

    @Tool(
        "jargon_boost_run",
        # 这个工具会写库并触发一批 LLM 推断，命令入口有 permission="operator"，
        # 工具通道以前没有任何门槛（任何会话都能诱导模型调用）。默认改成仅主人可用。
        permission="operator",
        description=(
            "手动把已经出现多次、但还没到主程序推断阈值的黑话词条 count 补到首推档，"
            "让主程序尽快完成含义确认。只有主人明确要求时才调用；"
            "dry_run=true 时只检查不写入。"
        ),
        parameters=[
            ToolParameterInfo(
                name="dry_run",
                param_type=ToolParamType.BOOLEAN,
                description="只检查会补哪些词，不真的修改 count",
                required=False,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description=f"最多补几条，默认 {DEFAULT_MANUAL_LIMIT}",
                required=False,
            ),
        ],
    )
    async def handle_manual_boost(
        self,
        dry_run: bool = False,
        limit: int = DEFAULT_MANUAL_LIMIT,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """工具入口：手动扫描并补 count。"""

        session_id = str(kwargs.get("stream_id") or "").strip()
        if not self._is_active():
            return {"success": False, "content": "黑话加速器已在配置里关闭。"}
        report = await self._manual_boost(
            limit=limit,
            dry_run=dry_run or self.config.boost.dry_run,
            session_id=session_id,
        )
        return {"success": True, "content": report}

    @Command(
        "jargon_boost",
        description="手动把够格的黑话词条 count 补到推断阈值",
        pattern=r"(?s)^/黑话加速(?:\s+(?P<args>.*))?$",
        # 手动入口会写库并触发一批 LLM 推断，只允许操作员执行；自动钩子路径不受影响。
        permission="operator",
    )
    async def handle_command(self, **kwargs: Any) -> Tuple[bool, Optional[str], int]:
        """命令入口：``/黑话加速`` 直接补，``/黑话加速 dry`` 只检查。"""

        if not self._is_active():
            return False, None, 1
        matched_groups: Dict[str, Any] = kwargs.get("matched_groups") or {}
        raw_args = str(matched_groups.get("args") or "").strip().lower()
        dry_run = raw_args in {"dry", "dry_run", "预演", "检查"} or self.config.boost.dry_run
        stream_id = str(kwargs.get("stream_id") or "").strip()
        try:
            report = await self._manual_boost(
                limit=DEFAULT_MANUAL_LIMIT,
                dry_run=dry_run,
                session_id=stream_id,
            )
        except Exception:
            # 内部细节（数据库报错、路径、仓库异常）只进日志；群里回一句人话就够了，
            # 原来把 exc 原文直接发进群聊
            self.ctx.logger.error("黑话加速命令执行失败", exc_info=True)
            report = "黑话加速执行失败，详细原因已经写进日志（含堆栈），请查看麦麦日志。"
            if stream_id:
                await self.ctx.send.text(report, stream_id)
            return False, None, 1
        if stream_id:
            await self.ctx.send.text(report, stream_id)
        return True, None, 1

    # ── 核心逻辑 ─────────────────────────────────────────────────

    def _is_active(self) -> bool:
        """判断插件总开关和分区开关是否都打开。"""

        return bool(self.config.plugin.enabled and self.config.boost.enabled)

    async def _boost_entries(self, session_id: str, entries: List[Any]) -> None:
        """遍历本次提取的词条，在预算内给够格的记录补 count。

        Args:
            session_id: 当前聊天流会话 ID。
            entries: 主程序本次提取并去重后的黑话条目列表。
        """

        config = self.config.boost
        budget = config.max_boosts_per_batch
        if budget <= 0:
            return
        contents = {content for content in (self._entry_content(entry) for entry in entries) if content}
        if not contents:
            return

        async with self._lock:
            # 一次查询取回候选窗口再在内存里匹配：原来每个词条发一次 database.query，
            # 主程序一次提取几十个词条就是几十次宿主往返（宿主侧是同步 SQLite，全部压在
            # 主事件循环上）。窗口里没覆盖到的词条再走一次精确查询，保证不漏。
            rows_by_content = await self._collect_rows(contents)
            rows = [
                row
                for row in rows_by_content.values()
                if self._is_boostable(row, session_id, config)
            ]
            # 先处理 count 更高的记录：它们离推断阈值最近，同样的预算更划算
            rows.sort(key=lambda row: self._as_int(row.get("count")), reverse=True)
            for row in rows:
                if budget <= 0:
                    break
                if await self._raise_count(row, config.target_count, dry_run=config.dry_run):
                    budget -= 1

    async def _collect_rows(self, contents: Set[str]) -> Dict[str, Dict[str, Any]]:
        """按 content 收集候选记录：首选一次窗口查询，没覆盖到的再精确补查。"""

        rows: Dict[str, Dict[str, Any]] = {}
        window = await self._query_candidates_window()
        for row in window:
            content = str(row.get("content") or "").strip()
            if content in contents:
                rows[content] = row
        for content in sorted(contents - set(rows)):
            for row in await self._query_rows(content):
                rows[content] = row
                break
        return rows

    async def _query_candidates_window(self) -> List[Dict[str, Any]]:
        """取一段"还没推断过"的黑话记录窗口，供内存匹配。"""

        result = await self.ctx.db.query(
            JARGON_MODEL_NAME,
            query_type="get",
            filters={"is_jargon": False, "last_inference_count": 0},
            order_by=["-count"],
            limit=JARGON_SCAN_WINDOW,
        )
        if isinstance(result, dict):
            raise RuntimeError(f"查询黑话候选失败：{result.get('error') or result!r}")
        if not isinstance(result, list):
            raise TypeError(f"database.query 返回形状异常: {type(result).__name__}")
        return [row for row in result if isinstance(row, dict)]

    async def _manual_boost(self, limit: int, dry_run: bool, session_id: str = "") -> str:
        """手动扫描一批候选记录并补 count，返回给用户看的回执。

        Args:
            limit: 最多补多少条。
            dry_run: 是否只记录不写入。
            session_id: 触发入口所在的会话；shared_globally=false 时用它限制扫描范围。

        Returns:
            str: 回执文本。
        """

        config = self.config.boost
        if not config.enabled:
            return "黑话加速器已在配置里关闭。"
        scan_limit = max(1, min(int(limit or DEFAULT_MANUAL_LIMIT), MAX_MANUAL_LIMIT))
        candidates = await self._scan_candidates(scan_limit, session_id)
        if not candidates:
            return f"没有需要提前确认的黑话（扫描上限 {scan_limit} 条）。"

        boosted: List[str] = []
        failed = 0
        async with self._lock:
            for row in candidates:
                old_count = self._as_int(row.get("count"))
                if await self._raise_count(row, config.target_count, dry_run=dry_run):
                    boosted.append(f"「{row.get('content')}」count {old_count} → {config.target_count}")
                else:
                    failed += 1

        mode = "预演" if dry_run else "已写入"
        lines = [f"黑话加速器（{mode}）：候选 {len(candidates)} 条，处理 {len(boosted)} 条，失败 {failed} 条。"]
        lines.extend(f"- {item}" for item in boosted[:MAX_BOOST_LOGS])
        if len(boosted) > MAX_BOOST_LOGS:
            lines.append(f"- ……还有 {len(boosted) - MAX_BOOST_LOGS} 条")
        return "\n".join(lines)

    async def _scan_candidates(self, limit: int, session_id: str = "") -> List[Dict[str, Any]]:
        """从数据库里挑出当前够格补 count 的记录。

        Args:
            limit: 最多返回多少条。
            session_id: 触发入口所在的会话；shared_globally=false 时用它限制扫描范围。

        Returns:
            List[Dict[str, Any]]: 候选记录列表。
        """

        rows = await self.ctx.db.query(
            JARGON_MODEL_NAME,
            query_type="get",
            filters={"is_jargon": False, "last_inference_count": 0},
            order_by=["-count"],
            limit=max(limit * 3, 50),
        )
        if isinstance(rows, dict):
            raise RuntimeError(f"查询黑话候选失败：{rows.get('error') or rows!r}")
        if not isinstance(rows, list):
            raise TypeError(f"database.query 返回形状异常: {type(rows).__name__}")
        config = self.config.boost
        candidates = [
            row
            for row in rows
            if isinstance(row, dict) and self._is_boostable(row, session_id, config)
        ]
        return candidates[:limit]

    @staticmethod
    def _entry_content(entry: Any) -> str:
        """从条目里取出黑话内容；兼容字典和纯字符串两种形态。"""

        if isinstance(entry, dict):
            return str(entry.get("content") or "").strip()
        if isinstance(entry, str):
            return entry.strip()
        return ""

    async def _query_rows(self, content: str) -> List[Dict[str, Any]]:
        """按内容查询黑话记录；查询失败时返回空列表。"""

        result = await self.ctx.db.query(
            JARGON_MODEL_NAME,
            query_type="get",
            filters={"content": content},
            limit=20,
        )
        if isinstance(result, dict):
            # 能力层出错时返回 {"success": False, "error": ...}：必须抛，不能当成"没查到"，
            # 否则数据库故障会伪装成"这个词还没被记录"。
            raise RuntimeError(f"查询黑话失败：{result.get('error') or result!r}")
        if not isinstance(result, list):
            raise TypeError(f"database.query 返回形状异常: {type(result).__name__}")
        return [row for row in result if isinstance(row, dict)]

    @staticmethod
    def _as_int(value: Any) -> int:
        """把数据库里的整数字段转成 int。

        count / last_inference_count 在模型里是非空整型列，出现非法值说明数据或契约已坏，
        这里直接抛错而不是折成 0——折成 0 会让该行"静默不合格"，问题永远查不出来。
        """

        return int(value)

    def _is_boostable(self, row: Dict[str, Any], session_id: str, config: BoostSectionConfig) -> bool:
        """判断一条黑话记录是否应该提前补 count。

        Args:
            row: 数据库记录字典。
            session_id: 当前聊天流会话 ID；手动扫描时传空字符串。
            config: 提前确认配置。

        Returns:
            bool: 需要补 count 时返回 True。
        """

        if row.get("id") is None:
            return False
        if not self._row_in_scope(row, session_id, config):
            return False
        if str(row.get("created_by") or "") == MANUAL_CREATOR:
            return False
        if bool(row.get("is_complete")) or bool(row.get("is_jargon")):
            return False
        # last_inference_count 非 0 表示主程序已经推断过：这里只负责「首推」，
        # 后续 8 / 25 / 100 档仍交给真实 count 决定，避免把已确认的词反复翻。
        if self._as_int(row.get("last_inference_count")) != 0:
            return False
        count = self._as_int(row.get("count"))
        return config.min_count <= count < config.target_count

    @staticmethod
    def _row_in_scope(row: Dict[str, Any], session_id: str, config: BoostSectionConfig) -> bool:
        """判断记录是否属于当前会话语义范围。

        主程序匹配记录时用的是配置解析后的「共享组会话集合」；插件拿不到那份解析结果，
        因此按配置走两条路：全局共享（与 `*/*` 组等价）时全部放行；
        否则只认当前会话和 is_global 记录，共享组场景会保守地跳过。
        """

        if config.shared_globally:
            return True
        if bool(row.get("is_global")):
            return True
        raw_session_dict = row.get("session_id_dict")
        if not raw_session_dict or not session_id:
            return False
        try:
            session_dict = json.loads(raw_session_dict)
        except (TypeError, ValueError):
            return False
        return isinstance(session_dict, dict) and session_id in session_dict

    async def _raise_count(self, row: Dict[str, Any], target_count: int, *, dry_run: bool = False) -> bool:
        """把单条记录的 count 补到目标值。

        Args:
            row: 数据库记录字典。
            target_count: 目标 count 值。
            dry_run: 只记录不写入。

        Returns:
            bool: 处理成功时返回 True（dry_run 也算成功）。
        """

        jargon_id = row.get("id")
        old_count = self._as_int(row.get("count"))
        content = str(row.get("content") or "")
        if dry_run:
            self.ctx.logger.info(
                "黑话加速器（预演）：「%s」count %s → %s（未写入）",
                content,
                old_count,
                target_count,
            )
            return True
        # 命中行数当唯一事实：宿主 db_update 内部会把异常吞成 0，所以必须看返回值。
        # filters 里带上读到的旧 count 做 CAS，避免把宿主刚 +1 的结果覆盖回更小的值。
        affected = await self.ctx.db.query(
            JARGON_MODEL_NAME,
            query_type="update",
            data={"count": target_count},
            filters={"id": jargon_id, "count": old_count},
        )
        if not isinstance(affected, int) or affected != 1:
            raise RuntimeError(
                f"黑话「{content}」(id={jargon_id}) 补 count 未生效："
                f"受影响行数 {affected!r}，期望 1（读取时 count={old_count}）"
            )
        self.ctx.logger.info(
            "黑话加速器：「%s」count %s → %s（下一次提取将触发主程序推断）",
            content,
            old_count,
            target_count,
        )
        return True


def create_plugin() -> MaiBotPlugin:
    """插件工厂函数，由 Runner 在加载插件时调用。"""

    return JargonBoostPlugin()
