"""黑话加速器插件的单元测试。

用标准库 unittest；插件模块按 Runner 的方式（importlib + 子模块搜索路径）加载，
这样 plugin.py 里的相对导入才可用。不导入宿主 ``src.*``，避免连带启动整个 MaiBot：
这里只验证插件自己的判定逻辑和对 database / send 能力的调用契约。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import IsolatedAsyncioTestCase

import importlib.util
import json
import sys
import tempfile

from pydantic import ValidationError

from maibot_sdk import PluginContext, PluginPaths

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
MODULE_NAME = PLUGIN_DIR.name
PLUGIN_ID = _MANIFEST["id"]
MANIFEST_VERSION = _MANIFEST["version"]

SESSION_ID = "session-aaaa"
OTHER_SESSION = "session-bbbb"
GROUP_NAME = "测试群"


def _load_plugin_module() -> Any:
    """按 Runner 的加载方式导入插件模块。"""

    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin_module()


def _row(
    content: str = "脑婆",
    *,
    row_id: int = 1,
    count: int = 2,
    last_inference_count: int = 0,
    is_jargon: bool = False,
    is_complete: bool = False,
    created_by: str = "AI",
    is_global: bool = False,
    sessions: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """构造一条 Jargon 记录字典。"""

    session_dict = sessions if sessions is not None else {SESSION_ID: 1}
    return {
        "id": row_id,
        "content": content,
        "count": count,
        "last_inference_count": last_inference_count,
        "is_jargon": is_jargon,
        "is_complete": is_complete,
        "created_by": created_by,
        "is_global": is_global,
        "session_id_dict": json.dumps(session_dict, ensure_ascii=False),
        "meaning": "",
    }


class FakeHost:
    """替身宿主：只用内存表模拟 Jargon 的 database.query 和 send.text。"""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []
        self.sent: List[Tuple[str, str]] = []
        self.queries: List[Dict[str, Any]] = []

    async def rpc(
        self,
        method: str,
        plugin_id: str,
        payload: Dict[str, Any],
        timeout_ms: Optional[int] = None,
    ) -> Any:
        """接受 sdk 的 cap.call，并按能力名分发。"""

        del timeout_ms
        if method != "cap.call":
            raise AssertionError(f"未预期的 host 方法: {method}")
        capability = str(payload.get("capability") or "")
        args = payload.get("args") or {}

        if capability == "database.query":
            return self._handle_database_query(args)
        if capability == "send.text":
            self.sent.append((str(args.get("stream_id")), str(args.get("text"))))
            return {"success": True, "sent": True}
        raise AssertionError(f"未预期的能力调用: {capability}")

    def _handle_database_query(self, args: Dict[str, Any]) -> Any:
        """按 query_type 处理数据库能力调用。"""

        self.queries.append(dict(args))
        query_type = str(args.get("query_type") or "get")
        filters: Dict[str, Any] = args.get("filters") or {}

        if query_type == "get":
            matched = [row for row in self.rows if self._matches(row, filters)]
            # 宿主先按 order_by 排序再截断，假实现也必须照做，否则「取 count 最高的 N 行」测不出来
            order_by = args.get("order_by") or []
            for key in reversed(order_by):
                if not isinstance(key, str) or not key:
                    continue
                descending = key.startswith("-")
                field = key[1:] if descending else key
                matched.sort(key=lambda row: row.get(field) or 0, reverse=descending)
            limit = args.get("limit")
            if isinstance(limit, int) and limit > 0:
                matched = matched[:limit]
            return [dict(row) for row in matched]

        if query_type == "update":
            data: Dict[str, Any] = args.get("data") or {}
            # 与宿主 database_service.db_update 一致：先按条件取命中行，再写入，返回命中行数
            affected = [row for row in self.rows if self._matches(row, filters)]
            for row in affected:
                row.update(data)
            self.updates.append({"data": dict(data), "filters": dict(filters)})
            return len(affected)

        return {"success": False, "error": f"未支持 query_type: {query_type}"}

    @staticmethod
    def _matches(row: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        """按等值条件匹配记录（与宿主 database_service 的行为一致）。"""

        return all(row.get(key) == value for key, value in filters.items())


class PluginTestCase(IsolatedAsyncioTestCase):
    """带替身宿主与临时目录的测试基类。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.host = FakeHost()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def build_plugin(self, **boost_overrides: Any) -> Any:
        """构造插件实例并完成上下文、配置与 on_load 注入。"""

        context = PluginContext(
            plugin_id=PLUGIN_ID,
            rpc_call=self.host.rpc,
            paths=PluginPaths(data_dir=self.tmp_dir, runtime_dir=self.tmp_dir),
        )
        plugin = PLUGIN_MODULE.JargonBoostPlugin()
        plugin._set_context(context)
        plugin.set_plugin_config(self.build_config(**boost_overrides))
        await plugin.on_load()
        return plugin

    @staticmethod
    def build_config(**boost_overrides: Any) -> Dict[str, Any]:
        """构造插件配置字典；只覆盖传入的 boost 字段。"""

        boost: Dict[str, Any] = {
            "enabled": True,
            "min_count": 2,
            "target_count": 3,
            "max_boosts_per_batch": 2,
            "shared_globally": False,
            "dry_run": False,
        }
        boost.update(boost_overrides)
        return {
            "plugin": {"enabled": True, "config_version": MANIFEST_VERSION},
            "boost": boost,
        }

    async def call_hook(self, plugin: Any, entries: List[Any], session_id: str = SESSION_ID) -> Dict[str, Any]:
        """模拟主程序调用提取钩子。"""

        return await plugin.before_persist(
            session_id=session_id,
            session_name=GROUP_NAME,
            entries=entries,
        )


class HookBoostTests(PluginTestCase):
    """钩子路径的判定与写入。"""

    async def test_raises_count_for_eligible_row(self) -> None:
        self.host.rows = [_row(count=2)]
        plugin = await self.build_plugin()

        result = await self.call_hook(plugin, [{"content": "脑婆", "evidence_messages": []}])

        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(self.host.rows[0]["count"], 3)
        self.assertEqual(len(self.host.updates), 1)
        # filters 带上读到的旧 count 做 CAS，避免把宿主刚 +1 的结果覆盖回更小的值
        self.assertEqual(self.host.updates[0]["filters"], {"id": 1, "count": 2})

    async def test_skips_manual_creator(self) -> None:
        self.host.rows = [_row(created_by="MANUAL")]
        plugin = await self.build_plugin()

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.rows[0]["count"], 2)
        self.assertEqual(self.host.updates, [])

    async def test_skips_already_inferred(self) -> None:
        self.host.rows = [_row(last_inference_count=4)]
        plugin = await self.build_plugin()

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.rows[0]["count"], 2)
        self.assertEqual(self.host.updates, [])

    async def test_skips_when_count_reaches_target(self) -> None:
        self.host.rows = [_row(count=3)]
        plugin = await self.build_plugin()

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.rows[0]["count"], 3)
        self.assertEqual(self.host.updates, [])

    async def test_skips_when_count_below_min(self) -> None:
        self.host.rows = [_row(count=1)]
        plugin = await self.build_plugin()

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.updates, [])

    async def test_skips_other_session_when_not_shared(self) -> None:
        self.host.rows = [_row(sessions={OTHER_SESSION: 1})]
        plugin = await self.build_plugin(shared_globally=False)

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.updates, [])

    async def test_shared_globally_accepts_other_session(self) -> None:
        self.host.rows = [_row(sessions={OTHER_SESSION: 1})]
        plugin = await self.build_plugin(shared_globally=True)

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.rows[0]["count"], 3)

    async def test_global_row_accepted_when_not_shared(self) -> None:
        self.host.rows = [_row(is_global=True, sessions={OTHER_SESSION: 1})]
        plugin = await self.build_plugin(shared_globally=False)

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.rows[0]["count"], 3)

    async def test_dry_run_does_not_write(self) -> None:
        self.host.rows = [_row(count=2)]
        plugin = await self.build_plugin(dry_run=True)

        await self.call_hook(plugin, [{"content": "脑婆"}])

        self.assertEqual(self.host.rows[0]["count"], 2)
        self.assertEqual(self.host.updates, [])

    async def test_batch_budget_limits_updates(self) -> None:
        self.host.rows = [
            _row("alpha", row_id=1, count=2),
            _row("beta", row_id=2, count=2),
            _row("gamma", row_id=3, count=2),
        ]
        plugin = await self.build_plugin(max_boosts_per_batch=2)

        await self.call_hook(plugin, [{"content": "alpha"}, {"content": "beta"}, {"content": "gamma"}])

        self.assertEqual(len(self.host.updates), 2)

    async def test_hook_failure_propagates(self) -> None:
        """失败必须冒到宿主：宿主管记堆栈 + 按 error_policy=SKIP 继续提取，插件自己包一层会吃掉 traceback。"""

        plugin = await self.build_plugin()

        async def broken_rpc(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("宿主失联")

        plugin._ctx._rpc_call = broken_rpc  # type: ignore[attr-defined]

        with self.assertRaises(RuntimeError):
            await self.call_hook(plugin, [{"content": "脑婆"}])


class ManualBoostTests(PluginTestCase):
    """手动扫描入口。"""

    async def test_manual_boost_reports_and_writes(self) -> None:
        self.host.rows = [_row("alpha", row_id=1, count=2), _row("beta", row_id=2, count=1)]
        plugin = await self.build_plugin()

        report = await plugin._manual_boost(limit=10, dry_run=False, session_id=SESSION_ID)

        self.assertIn("候选 1 条", report)
        self.assertIn("alpha", report)
        self.assertEqual(self.host.rows[0]["count"], 3)
        self.assertEqual(self.host.rows[1]["count"], 1)

    async def test_manual_boost_dry_run_keeps_data(self) -> None:
        self.host.rows = [_row("alpha", count=2)]
        plugin = await self.build_plugin()

        report = await plugin._manual_boost(limit=10, dry_run=True, session_id=SESSION_ID)

        self.assertIn("预演", report)
        self.assertEqual(self.host.rows[0]["count"], 2)
        self.assertEqual(self.host.updates, [])

    async def test_manual_boost_respects_limit(self) -> None:
        self.host.rows = [_row("alpha", row_id=1, count=2), _row("beta", row_id=2, count=2)]
        plugin = await self.build_plugin()

        await plugin._manual_boost(limit=1, dry_run=False, session_id=SESSION_ID)

        self.assertEqual(len(self.host.updates), 1)

    async def test_manual_boost_scoped_to_session_when_not_shared(self) -> None:
        """shared_globally=false 时手动扫描只处理当前会话的记录。"""

        self.host.rows = [
            _row("alpha", row_id=1, count=2, sessions={SESSION_ID: 1}),
            _row("beta", row_id=2, count=2, sessions={OTHER_SESSION: 1}),
        ]
        plugin = await self.build_plugin(shared_globally=False)

        report = await plugin._manual_boost(limit=10, dry_run=False, session_id=SESSION_ID)

        self.assertIn("候选 1 条", report)
        self.assertEqual(self.host.rows[0]["count"], 3)
        self.assertEqual(self.host.rows[1]["count"], 2)

    async def test_manual_boost_without_candidates(self) -> None:
        plugin = await self.build_plugin()

        report = await plugin._manual_boost(limit=10, dry_run=False, session_id=SESSION_ID)

        self.assertIn("没有需要提前确认的黑话", report)

    async def test_manual_boost_disabled(self) -> None:
        plugin = await self.build_plugin(enabled=False)

        report = await plugin._manual_boost(limit=10, dry_run=False, session_id=SESSION_ID)

        self.assertIn("已在配置里关闭", report)


class EntryAndCommandTests(PluginTestCase):
    """入口格式与命令回执。"""

    async def test_entry_content_accepts_dict_and_str(self) -> None:
        plugin = await self.build_plugin()

        self.assertEqual(plugin._entry_content({"content": " 脑婆 "}), "脑婆")
        self.assertEqual(plugin._entry_content(" 贴贴 "), "贴贴")
        self.assertEqual(plugin._entry_content(None), "")

    async def test_command_requires_operator_permission(self) -> None:
        """手动入口会写库并触发一批 LLM 推断，必须是 operator 权限。"""

        plugin = await self.build_plugin()

        command = next(c for c in plugin.get_components() if c.get("name") == "jargon_boost")

        self.assertEqual(command["metadata"].get("permission"), "operator")

    async def test_command_sends_receipt(self) -> None:
        self.host.rows = [_row("alpha", count=2)]
        plugin = await self.build_plugin()

        result = await plugin.handle_command(
            stream_id=SESSION_ID,
            matched_groups={"args": ""},
        )

        self.assertEqual(result, (True, None, 1))
        self.assertEqual(len(self.host.sent), 1)
        self.assertIn("黑话加速器", self.host.sent[0][1])

    async def test_command_dry_mode(self) -> None:
        self.host.rows = [_row("alpha", count=2)]
        plugin = await self.build_plugin()

        await plugin.handle_command(stream_id=SESSION_ID, matched_groups={"args": " dry "})

        self.assertIn("预演", self.host.sent[0][1])
        self.assertEqual(self.host.updates, [])

    async def test_command_skipped_when_disabled(self) -> None:
        plugin = await self.build_plugin(enabled=False)

        result = await plugin.handle_command(stream_id=SESSION_ID, matched_groups={"args": ""})

        self.assertEqual(result, (False, None, 1))
        self.assertEqual(self.host.sent, [])


class CountConstraintTests(PluginTestCase):
    """count 相关配置必须受宿主阈值约束，不能把推断档位一次跨过去。"""

    async def test_target_count_above_first_threshold_is_rejected(self) -> None:
        """target_count 最多到 3（宿主再 +1 = 首推阈值 4），再高会跨档甚至直接标成已确认。"""

        with self.assertRaises(ValidationError):
            await self.build_plugin(target_count=5)

    async def test_min_count_must_be_below_target(self) -> None:
        """min_count >= target_count 时一条都补不到，属于必然失效的配置。"""

        with self.assertRaises(ValidationError):
            await self.build_plugin(min_count=3, target_count=3)


class ErrorPathTests(PluginTestCase):
    """失败必须暴露，不能被降级成"没有候选"或"已成功"。"""

    async def test_capability_error_is_raised_not_swallowed(self) -> None:
        """database.query 返回 {"success": False} 时必须抛，不能当成"这个词没记录"。"""

        plugin = await self.build_plugin()

        # 直接让 FakeHost 返回能力错误字典（宿主 database_service 出错时的真实形状）
        original = self.host._handle_database_query

        def error_query(args: Dict[str, Any]) -> Any:
            if str(args.get("query_type")) == "update":
                return 1
            return {"success": False, "error": "database is locked"}

        self.host._handle_database_query = error_query  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                await self.call_hook(plugin, [{"content": "脑婆"}])
        finally:
            self.host._handle_database_query = original  # type: ignore[assignment]

    async def test_update_without_affected_row_is_raised(self) -> None:
        """UPDATE 命中 0 行（行被删/被宿主改过）时必须抛，不能打印"补成功"。"""

        self.host.rows = [_row(count=2)]
        plugin = await self.build_plugin()

        original = self.host._handle_database_query

        def no_match_update(args: Dict[str, Any]) -> Any:
            if str(args.get("query_type")) == "update":
                return 0
            return original(args)

        self.host._handle_database_query = no_match_update  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                await self.call_hook(plugin, [{"content": "脑婆"}])
        finally:
            self.host._handle_database_query = original  # type: ignore[assignment]

        self.assertEqual(self.host.rows[0]["count"], 2)  # 没有被写坏

    async def test_self_check_raises_on_capability_error(self) -> None:
        """on_load 的数据库自检在能力报错时必须让加载失败，而不是打印"自检通过"。"""

        original = self.host._handle_database_query
        self.host._handle_database_query = lambda args: {"success": False, "error": "boom"}  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                await self.build_plugin()
        finally:
            self.host._handle_database_query = original  # type: ignore[assignment]
