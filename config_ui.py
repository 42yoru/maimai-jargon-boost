"""WebUI 配置页的中文展示布局。

只重组 SDK 生成的 Schema（分块、中文标签、提示、页签），**不改配置模型、
不改默认值、也不改 TOML 里的真实路径**：每个展示块的 ``name`` 始终指向原有的配置节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class _Section:
    """一个虚拟展示块。"""

    source: str
    title: str
    description: str
    fields: Dict[str, str]
    collapsed: bool = False


_SECTIONS: Dict[str, _Section] = {
    "boost": _Section(
        source="boost",
        title="提前确认",
        description=(
            "把已经抽到多次、但还没到主程序推断阈值的黑话 count 提前补到首推档；"
            "随后主程序会用原有的三段推断把含义确认下来。"
        ),
        fields={
            "enabled": "启用提前确认",
            "min_count": "最少出现次数",
            "target_count": "补到的次数",
            "max_boosts_per_batch": "每批最多补几条",
            "shared_globally": "黑话全局共享",
            "dry_run": "只记录不写入",
        },
    ),
    "plugin": _Section(
        source="plugin",
        title="插件开关",
        description="关闭后钩子仍然挂载，但不会做任何事。",
        fields={
            "enabled": "启用插件",
            "config_version": "配置版本",
        },
        collapsed=True,
    ),
}

_TABS = {
    "basic": ("常用设置", ("boost",)),
    "advanced": ("高级", ("plugin",)),
}

_HINTS: Dict[str, str] = {
    "boost.enabled": "关掉后不再补 count；已经补过的数值不会自动回滚。",
    "boost.min_count": "词条至少被抽到几次才有资格补；2 表示第二次出现就会补。",
    "boost.target_count": "补到的 count 值。主程序随后会再 +1，所以 3 表示刚好落到首推阈值 4。",
    "boost.max_boosts_per_batch": "每次提取最多补几条，用来控制同时触发的推断数量（每条推断会调用 3 次 LLM）。",
    "boost.shared_globally": "与主程序的黑话共享组保持一致：bot 配置里是 `*/*` 全局共享时保持开启；只共享部分群时关闭，插件就只处理当前会话的记录。",
    "boost.dry_run": "开启后只写日志、不修改 count，用来观察到底会补哪些词。",
    "plugin.enabled": "插件总开关。",
    "plugin.config_version": "由插件自己维护，不要手改。",
}


def _build_field(source: str, name: str, template: Dict[str, Any], label: str, order: int) -> Dict[str, Any]:
    """在 SDK 给出的字段定义上补中文标签、提示与顺序。"""

    path = f"{source}.{name}"
    hint = _HINTS.get(path, template.get("description") or "")
    entry = dict(template)
    entry.update(
        label=label,
        hint=hint,
        description=hint,
        order=order,
    )
    return entry


def build_config_ui_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """重组 SDK Schema：分出页签、中文标签与提示，其余保持原样。

    Args:
        schema: SDK ``build_config_schema()`` 生成的原始 Schema。

    Returns:
        Dict[str, Any]: 重组后的 Schema；字段默认值与类型保持原样。
    """

    source_sections: Dict[str, Any] = schema.get("sections") or {}
    sections: Dict[str, Any] = {}
    tabs: list[Dict[str, Any]] = []

    for tab_id, (tab_title, section_ids) in _TABS.items():
        tabs.append({"id": tab_id, "title": tab_title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _SECTIONS[section_id]
            template_section = source_sections.get(spec.source)
            if template_section is None:
                continue
            template_fields = template_section.get("fields") or {}
            fields: Dict[str, Any] = {}
            for order, (field_name, label) in enumerate(spec.fields.items()):
                template = template_fields.get(field_name)
                if template is None:
                    continue
                fields[field_name] = _build_field(spec.source, field_name, template, label, order)
            if not fields:
                continue
            # 字典键只标识展示分块，name 才是 WebUI 读写配置的真实节名
            sections[section_id] = {
                **template_section,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": spec.collapsed,
                "order": len(sections),
                "fields": fields,
            }

    return {**schema, "sections": sections, "layout": {"type": "tabs", "tabs": tabs}}


__all__ = ["build_config_ui_schema"]
