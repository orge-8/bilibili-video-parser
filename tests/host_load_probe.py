# -*- coding: utf-8 -*-
"""宿主级加载门禁：用真机 MaiBot 官方 PluginLoader 加载本插件并核对组件注册。

为什么需要它：`tests/smoke_test.py` 用的是自建 fakehost，走不到宿主的两道硬闸——
  1. ManifestValidator 按 host_version 校验 `host_application.min/max_version`；
  2. `PluginLoader._validate_sdk_plugin_contract()` 要求 on_load / on_unload /
     on_config_update 都被覆写，且 `get_config_reload_subscriptions()` 可调用。
这两道闸只在真机 Host 上跑得出来，本地 fakehost 永远绿。

用法（用真机 Host 自带的 Python，不是 devkit venv）：
    <host>/python-env/python.exe tests/host_load_probe.py
    <host>/python-env/python.exe tests/host_load_probe.py <MaiBot 根目录>
环境变量 `MAIBOT_HOST_ROOT` 可替代位置参数。
找不到 Host 时脚本以退出码 0 跳过（本地无实例不该算失败）。

只加载本插件，不触碰同目录下其它插件，避免副作用。
退出码：0 = PASS/跳过，2 = 未发现插件，3 = 加载失败，4 = 组件数不符。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

PLUGIN_ID = "org.mai-mai.bilibili-video-parser"
EXPECTED = {"COMMAND": 1, "TOOL": 1, "HOOK_HANDLER": 3}

DEFAULT_HOST_ROOT = (
    r"C:\Users\38160\AppData\Roaming\MaiBotOneKeyDesktop\e6a2be8e69ae\modules\MaiBot"
)


def iter_host_candidates() -> list[Path]:
    """按优先级列出可能的 MaiBot 根目录。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("host_root_pos", nargs="?", default="")
    parser.add_argument("--host-root", default="")
    known, _ = parser.parse_known_args()

    candidates: list[Path] = []
    for raw in (
        known.host_root,
        os.environ.get("MAIBOT_HOST_ROOT", ""),
        known.host_root_pos,
        DEFAULT_HOST_ROOT,
    ):
        if raw:
            candidates.append(Path(raw))

    # OneKey 的实例目录名（如 e6a2be8e69ae）不固定，兜底扫一遍
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    for root in (roaming / "MaiBotOneKeyDesktop", roaming / "MaiBotOneKey"):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            nested = child / "modules" / "MaiBot"
            if (nested / "src").is_dir() and (nested / "plugins").is_dir():
                candidates.append(nested)

    return candidates


def resolve_host_root() -> Path | None:
    """定位真机 MaiBot 根目录（含 src/plugin_runtime 与 plugins/）。"""
    for candidate in iter_host_candidates():
        if (candidate / "src" / "plugin_runtime").is_dir() and (candidate / "plugins").is_dir():
            return candidate.resolve()
    return None


def read_host_version(host_root: Path) -> str:
    """从 pyproject.toml 读取宿主版本号。"""
    pyproject = host_root / "pyproject.toml"
    if pyproject.is_file():
        matched = re.search(
            r'^\s*version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        if matched:
            return matched.group(1)
    return "unknown"


def main() -> int:
    host_root = resolve_host_root()
    if host_root is None:
        print("[SKIP] 未找到真机 MaiBot 根目录（可用位置参数或 MAIBOT_HOST_ROOT 指定）")
        return 0

    host_version = read_host_version(host_root)
    print(f"[host] {host_root}")
    print(f"[host] version = {host_version}")

    sys.path.insert(0, str(host_root))
    from src.plugin_runtime.runner.plugin_loader import PluginLoader  # noqa: E402

    loader = PluginLoader(host_version=host_version)
    candidates, duplicates = loader.discover_candidates([str(host_root / "plugins")])
    print(f"[discover] {len(candidates)} 个候选，重复 id {len(duplicates)} 个")

    if PLUGIN_ID not in candidates:
        print(f"[FAIL] 未发现目标插件 {PLUGIN_ID}")
        print("failed:", json.dumps(loader.failed_plugins, ensure_ascii=False, indent=2))
        return 2

    meta = loader.load_candidate(PLUGIN_ID, candidates[PLUGIN_ID])
    if meta is None:
        print("[FAIL] load_candidate 返回 None（manifest 闸或 SDK 契约闸未过）")
        print("failed:", json.dumps(loader.failed_plugins, ensure_ascii=False, indent=2))
        return 3

    print(f"[loaded] {meta.plugin_id} v{meta.version} type={meta.plugin_type}")
    print(f"[loaded] capabilities = {meta.capabilities_required}")
    print(f"[loaded] instance = {type(meta.instance).__name__}")

    components = meta.instance.get_components()
    counts = dict(Counter(component["type"] for component in components))
    print(f"[components] {counts}")
    for component in components:
        handler = component["metadata"].get("handler_name")
        print(f"  [{component['type']}] {component['name']}  (handler={handler})")

    providers = [provider.get("name") for provider in meta.instance.get_llm_providers()]
    print(f"[llm_providers] {providers}")
    print(f"[config_reload_subs] {meta.instance.get_config_reload_subscriptions()}")

    foreign_failures = {
        pid: reason for pid, reason in loader.failed_plugins.items() if pid != PLUGIN_ID
    }
    if foreign_failures:
        print(f"[note] 同目录另有 {len(foreign_failures)} 个插件加载失败（与本插件无关）：")
        for pid, reason in foreign_failures.items():
            print(f"  - {pid}: {reason}")

    if counts != EXPECTED:
        print(f"[FAIL] 组件数与预期不符：得到 {counts}，预期 {EXPECTED}")
        return 4

    print("[PASS] 宿主加载门禁通过：manifest 兼容 + SDK 契约 + 组件注册全部符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
