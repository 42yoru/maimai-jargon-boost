"""黑话提前确认插件（jargon_boost）。

主程序的黑话含义推断要等某条黑话的 count 涨到 4 / 8 / 25 / 100 才触发，
但很多词条在聊天里只被抽到一两次就沉底了，于是长期停在「未确认」。

本插件不改主程序，只在每次黑话提取（`jargon.extract.before_persist`）写库前，
把「已经有足够证据、但从未推断过」的记录 count 提前补到首推阈值：
主程序随后会自己把 count 再 +1，并通过 `_should_infer_meaning` 判定，
用原有的三段推断、原有的 prompt 和原有的含义写回流程完成确认。

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

from typing import Any, Dict, List

import asyncio
import json

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

JARGON_MODEL_NAME = "Jargon"
"""主程序 Jargon 数据模型名，对应 database_model 中的类名。"""

MANUAL_CREATOR = "MANUAL"
"""手动录入来源；这类记录主程序不会推断，插件也不应干预。"""


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class BoostSectionConfig(PluginConfigBase):
    """黑话提前确认参数。"""

    __ui_label__ = "提前确认"
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
        description="补到的 count 值；主程序随后会再 +1，所以 3 表示刚好落到首推阈值 4",
    )
    max_boosts_per_batch: int = Field(
        default=2,
        ge=1,
        le=10,
        description="每次提取最多补几条，控制同时触发的推断数量（每次推断会调用 3 次 LLM）",
    )


class JargonBoostConfig(PluginConfigBase):
    """黑话提前确认插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    boost: BoostSectionConfig = Field(default_factory=BoostSectionConfig)


class JargonBoostPlugin(MaiBotPlugin):
    """在每次黑话提取写库前，把够格的记录 count 提前补到首推阈值。"""

    config_model = JargonBoostConfig

    def __init__(self) -> None:
        super().__init__()
        # 多个聊天流可能同时触发提取，串行化数据库读写，避免同一条记录被并发补两次。
        self._lock = asyncio.Lock()

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
        try:
            if session_id and isinstance(entries, list) and self._is_active():
                await self._boost_entries(session_id, entries)
        except Exception as exc:
            # 任何失败都只记录，不打断主程序的黑话提取流程。
            self.ctx.logger.warning("黑话提前确认失败，已跳过本次提取：%s", exc)
        return {"action": "continue"}

    def _is_active(self) -> bool:
        """判断插件总开关和分区开关是否都打开。"""

        return bool(self.config.plugin.enabled and self.config.boost.enabled)

    async def on_load(self) -> None:
        """插件加载完成后输出一次生效参数，并自检插件到宿主的数据库能力回环。"""

        config = self.config.boost
        self.ctx.logger.info(
            "黑话提前确认已加载：min_count=%s target_count=%s max_boosts_per_batch=%s",
            config.min_count,
            config.target_count,
            config.max_boosts_per_batch,
        )
        # 只读自检：确认钩子里要用的 database.query 能力能通，免得等到真正提取时才发现回环问题。
        try:
            rows = await self.ctx.db.query(JARGON_MODEL_NAME, query_type="get", limit=1)
        except Exception as exc:
            self.ctx.logger.warning("数据库回环自检失败：%s", exc)
            return
        row_count = len(rows) if isinstance(rows, list) else 0
        self.ctx.logger.info("数据库回环自检通过：database.query 可用（样例记录 %s 条）", row_count)

    async def on_unload(self) -> None:
        """插件卸载回调；本插件不持有需要落盘的状态。"""

        self.ctx.logger.info("黑话提前确认已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新回调；参数由宿主传入，插件读取时始终以 self.config 为准。"""

        del scope, config_data, version
        self.ctx.logger.info("黑话提前确认配置已更新")

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
        async with self._lock:
            for entry in entries:
                if budget <= 0:
                    break
                content = self._entry_content(entry)
                if not content:
                    continue
                for row in await self._query_rows(content):
                    if budget <= 0:
                        break
                    if not self._is_boostable(row, session_id, config):
                        continue
                    if await self._raise_count(row, config.target_count):
                        budget -= 1

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
            # 能力层出错时返回 {"success": False, "error": ...}，这里只当没查到。
            if not result.get("success"):
                return []
            return [result]
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        return []

    @staticmethod
    def _as_int(value: Any) -> int:
        """把数据库里的整数字段安全转成 int；非法值按 0 处理。"""

        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _is_boostable(self, row: Dict[str, Any], session_id: str, config: BoostSectionConfig) -> bool:
        """判断一条黑话记录是否应该提前补 count。

        Args:
            row: 数据库记录字典。
            session_id: 当前聊天流会话 ID。
            config: 提前确认配置。

        Returns:
            bool: 需要补 count 时返回 True。
        """

        if row.get("id") is None:
            return False
        if not self._row_in_scope(row, session_id):
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
    def _row_in_scope(row: Dict[str, Any], session_id: str) -> bool:
        """判断记录是否属于当前会话（或全局共享）。

        主程序匹配记录时还会考虑黑话共享组；插件没有那份配置视图，
        因此这里只认精确会话和全局记录，共享组场景会保守地跳过。
        """

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

    async def _raise_count(self, row: Dict[str, Any], target_count: int) -> bool:
        """把单条记录的 count 补到目标值。

        Args:
            row: 数据库记录字典。
            target_count: 目标 count 值。

        Returns:
            bool: 补成功时返回 True。
        """

        jargon_id = row.get("id")
        old_count = self._as_int(row.get("count"))
        try:
            await self.ctx.db.query(
                JARGON_MODEL_NAME,
                query_type="update",
                data={"count": target_count},
                filters={"id": jargon_id},
            )
        except Exception as exc:
            self.ctx.logger.warning("黑话「%s」补 count 失败：%s", row.get("content"), exc)
            return False
        self.ctx.logger.info(
            "黑话提前确认：「%s」count %s → %s（下一次提取将触发主程序推断）",
            row.get("content"),
            old_count,
            target_count,
        )
        return True


def create_plugin() -> MaiBotPlugin:
    """插件工厂函数，由 Runner 在加载插件时调用。"""

    return JargonBoostPlugin()
