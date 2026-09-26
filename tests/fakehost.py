#!/usr/bin/env python3
"""FakeHost —— 不启动 MaiBot 也能跑插件生命周期的冒烟脚手架。

由 maibot-plugin-prep 生成进插件的 tests/ 目录，也可单独参考。

核心思路: 造假 Host 的 PluginContext，在 rpc_call 里拦截能力调用，
即可直接 await 插件的 on_load / @Command / @Tool / on_unload。

    ctx = build_context("github.me.demo", rpc_call=FakeHost().rpc_call)
    plugin = create_plugin()
    plugin._set_context(ctx)
    plugin.set_plugin_config(get_default_config(MyPluginConfig))
    await plugin.on_load()
    ...
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------- 假路径 / 假上下文

@dataclass
class FakePaths:
    """替代 self.ctx.paths：一律落在临时目录，测试绝不碰真实数据。"""
    root: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="maibot-fake-")))

    @property
    def data_dir(self) -> Path:
        p = self.root / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def runtime_dir(self) -> Path:
        p = self.root / "runtime"
        p.mkdir(parents=True, exist_ok=True)
        return p


class FakeHost:
    """拦截 ctx.* 的能力调用，返回可控假数据，并记录全部调用便于断言。"""

    #: capability -> 默认返回值
    DEFAULT_RETURNS: dict[str, Any] = {
        "chat.open_session": {"stream": {"stream_id": "fake-stream", "session_id": "fake-stream"},
                              "created": True},
        "chat.get_stream_by_group_id": {"stream_id": "fake-stream"},
        "message.get_recent": [],
        "message.get_by_id": None,
        "message.get_by_time_in_chat": [],
        "message.build_readable": "",
        "message.count_new": 0,
        "person.get_id": "fake-person-id",
        "person.get_value": None,
        "llm.generate": {"success": True, "response": "fake-llm-response", "model": "fake-model"},
        "llm.embed": {"embedding": [0.0] * 8},
        "llm.get_available_models": ["utils", "replyer"],
        "config.get": None,
        "database.query": [],
        "database.get": [],
        "database.count": 0,
        "database.save": True,
        "database.delete": True,
        "knowledge.search": [],
        "tool.get_definitions": [],
        "render.html2png": {"image_base64": "", "mime_type": "image/png", "width": 1, "height": 1},
        "emoji.get_random": [],
        "emoji.get_count": 0,
        "frequency.get_current_talk_value": 1.0,
    }

    def __init__(self, plugin_id: str = "fake.plugin", returns: dict[str, Any] | None = None,
                 paths: FakePaths | None = None) -> None:
        self.plugin_id = plugin_id
        self.paths = paths or FakePaths()
        self.returns = dict(self.DEFAULT_RETURNS)
        self.returns.update(returns or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # 插件侧所有 ctx.* 最终都走这里。
    # 真实 SDK 链路: ctx.send.text(...) -> call_capability("send.text", ...)
    #   -> call_host_method("cap.call", payload={"capability": "send.text", "args": {...}})
    #   -> await self._rpc_call("cap.call", plugin_id, payload)
    # 因此 rpc_call 必须是 async，签名 (method, plugin_id, payload) 位置传参。
    async def rpc_call(self, method: str, plugin_id: str = "",
                       payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        # method = "cap.call"；真实能力名在 payload["capability"]，参数在 payload["args"]
        kw: dict[str, Any] = dict(payload or {})
        capability = kw.get("capability") or method
        args = kw.get("args") or {}
        self.calls.append((capability, args))
        if capability.startswith("send."):
            if args.get("return_details"):
                return {"sent": True, "message_id": "fake-message-id"}
            return True
        if capability.startswith(("api.", "adapter.")):
            return {"status": "ok", "retcode": 0, "data": {}, "echo": "fake"}
        if capability.startswith("statistics.local."):
            return {"series": {"timestamps": [], "values_by_key": {}, "total": 0}}
        if capability in self.returns:
            return self.returns[capability]
        return {"success": True, "result": None}

    #: 便捷断言: 取某能力的调用记录
    def calls_of(self, capability: str) -> list[dict[str, Any]]:
        return [kw for cap, kw in self.calls if cap == capability]

    @property
    def sent_texts(self) -> list[str]:
        return [kw.get("text") or kw.get("content") for kw in self.calls_of("send.text")]


def _import_first(candidates: list[str]) -> Any:
    """按 "module:attr" 依次尝试导入，全部失败返回 None。"""
    for target in candidates:
        module_name, attr = target.split(":", 1)
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            continue
        obj = getattr(mod, attr, None)
        if obj is not None:
            return obj
    return None


def build_context(plugin_id: str, rpc_call: Callable[..., Any] | None = None,
                  paths: Any = None) -> Any:
    """构造插件可用的 ctx：装了 SDK 用真实 PluginContext，否则用 stub。

    真实 SDK 形态: PluginContext(plugin_id, rpc_call, PluginPaths(...))
    """
    paths = paths or FakePaths()
    rpc_call = rpc_call or FakeHost(plugin_id).rpc_call
    logger = logging.getLogger(f"plugin.{plugin_id}")

    ctx_cls = _import_first(["maibot_sdk.context:PluginContext", "maibot_sdk:PluginContext"])
    if ctx_cls is not None:
        for attempt in (
            lambda: ctx_cls(plugin_id, rpc_call, paths),
            lambda: ctx_cls(plugin_id=plugin_id, rpc_call=rpc_call, paths=paths),
        ):
            try:
                return attempt()
            except Exception:
                continue
    # 无 SDK 时的 stub（够跑生命周期与直接调用的组件方法）
    return types.SimpleNamespace(plugin_id=plugin_id, rpc_call=rpc_call, paths=paths, logger=logger)


# ---------------------------------------------------------------- 加载插件 / 默认配置

def load_plugin_module(plugin_dir: str | Path, module_name: str = "plugin_under_test"):
    """按文件路径导入插件的 plugin.py。"""
    plugin_dir = Path(plugin_dir)
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "plugin.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {plugin_dir / 'plugin.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def get_default_config(config_model: Any) -> dict[str, Any]:
    """从配置模型取默认配置（pydantic 模型走 model_dump，否则回退空 dict）。"""
    if config_model is None:
        return {}
    try:
        return config_model().model_dump()
    except Exception:
        return {}


def bind_context(plugin: Any, ctx: Any, config: dict[str, Any] | None = None) -> None:
    """把假上下文与默认配置注入插件实例。"""
    if hasattr(plugin, "_set_context"):
        plugin._set_context(ctx)
    else:
        plugin.ctx = ctx
    if config is not None and hasattr(plugin, "set_plugin_config"):
        plugin.set_plugin_config(config)


def smoke(plugin_dir: str | Path, commands: list[str] | None = None,
          tools: list[str] | None = None, **command_kwargs: Any) -> dict[str, Any]:
    """一次性跑完 生命周期 -> 命令 -> 工具 -> 卸载，返回结果摘要。

    commands / tools 传入的是**方法名**（如 cmd_ping / tool_hello）。
    """
    module = load_plugin_module(plugin_dir)
    plugin = module.create_plugin()
    host = FakeHost()
    ctx = build_context(getattr(module, "__plugin_id__", "fake.plugin"), rpc_call=host.rpc_call)

    config_model = getattr(type(plugin), "config_model", None)
    bind_context(plugin, ctx, get_default_config(config_model))

    async def _run() -> dict[str, Any]:
        result: dict[str, Any] = {"on_load": None, "commands": {}, "tools": {}, "on_unload": None}
        await plugin.on_load()
        result["on_load"] = "ok"
        for name in commands or []:
            fn = getattr(plugin, name, None)
            if fn is None:
                result["commands"][name] = "MISSING"
                continue
            result["commands"][name] = await fn(**command_kwargs)
        for name in tools or []:
            fn = getattr(plugin, name, None)
            if fn is None:
                result["tools"][name] = "MISSING"
                continue
            result["tools"][name] = await fn(**command_kwargs)
        await plugin.on_unload()
        result["on_unload"] = "ok"
        return result

    result = asyncio.run(_run())
    result["calls"] = [cap for cap, _ in host.calls]
    result["sent_texts"] = host.sent_texts
    return result


if __name__ == "__main__":
    import pprint

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    pprint.pprint(smoke(target))
