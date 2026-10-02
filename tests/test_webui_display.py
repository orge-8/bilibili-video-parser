# -*- coding: utf-8 -*-
"""WebUI 配置 Schema 显示层回归测试：走插件实例的真实链路。

真机 Runner 调的就是 ``plugin.get_webui_config_schema``（异常被吞 →
空 Schema → 配置页整页空白），所以这里必须走插件实例，而不是直接调
SDK 生成器。
"""
import importlib
import pathlib
import sys

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR.parent))

_MOD = importlib.import_module(_PLUGIN_DIR.name)
if not hasattr(_MOD, "create_plugin"):
    # 包式加载（与真机一致）：__init__.py 可能只是引导文件，需下沉到 .plugin 子模块
    _MOD = importlib.import_module(f"{_PLUGIN_DIR.name}.plugin")
_PLUGIN = _MOD.create_plugin()


def _webui_sections() -> dict:
    schema = _PLUGIN.get_webui_config_schema(
        plugin_id=getattr(_MOD, "__plugin_id__", _PLUGIN_DIR.name),
        plugin_name=_PLUGIN_DIR.name,
        plugin_version="",
        plugin_description="",
        plugin_author="",
    )
    sections = (schema or {}).get("sections") or {}
    assert sections, "schema 无 sections：Runner 会渲染出整页空白"
    return sections


def test_sections_have_visible_fields():
    sections = _webui_sections()
    assert any(
        not f.get("hidden")
        for s in sections.values()
        for f in (s.get("fields") or {}).values()
    ), "所有字段均被隐藏，配置页会空白"


def test_no_object_fields():
    for s in _webui_sections().values():
        for f in (s.get("fields") or {}).values():
            assert not (f.get("type") == "object" or f.get("ui_type") == "json"), (
                "嵌套对象落 default 文本框会显示 [object Object]"
            )


def test_description_also_visible_as_hint():
    for s in _webui_sections().values():
        for f in (s.get("fields") or {}).values():
            if f.get("hidden"):
                continue
            if (f.get("description") or "").strip():
                assert (f.get("hint") or "").strip(), (
                    f"字段 {f.get('name')} 的说明在可视化模式不可见（需抄进 hint）"
                )


def test_labels_not_raw_keys():
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if f.get("hidden"):
                continue
            label = (f.get("label") or "").strip()
            assert label and label != fname, f"字段 {fname} 仍以英文键名当 label"


def test_section_titles_not_raw_keys():
    for name, s in _webui_sections().items():
        title = (s.get("title") or "").strip()
        assert title and title != name, f"节 {name} 仍以键名当标题"


def test_config_version_hidden_if_present():
    for s in _webui_sections().values():
        for fname, f in (s.get("fields") or {}).items():
            if fname == "config_version":
                assert f.get("hidden") is True, "config_version 应只在源代码模式可见"
