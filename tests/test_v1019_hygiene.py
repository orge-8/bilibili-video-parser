"""v1.0.19 审计卫生项 —— maibot-plugin-audit 第 10/11/12/14/18 项。

与安全回归（test_v1019_egress.py）分开：这里管「文档与代码是否自洽」
以及「注入块是否可能出现空/纯占位内容」——都属不报错但会安静出错的一类。
"""
import asyncio
import ast
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402
import plugin as plugin_module  # noqa: E402
from bili_video import VideoTarget  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = "org.mai-mai.bilibili-video-parser"


@pytest.fixture()
def plugin():
    module = load_plugin_module(PLUGIN_DIR)
    p = module.create_plugin()
    host = FakeHost(plugin_id=PLUGIN_ID)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    bind_context(p, ctx, get_default_config(getattr(type(p), "config_model", None)))
    return p


def _stub_client(info: dict):
    """只回答 get_video_info 的假 client，用来隔离注入块构造逻辑。"""
    class _Client:
        async def resolve_target(self, target):
            return target

        async def get_video_info(self, _target):
            return info

        async def get_ai_conclusion(self, _info):
            return None

        async def get_videoshot(self, _info):
            return None

        async def get_subtitle(self, _info):
            return None

        async def close(self):
            pass

    return _Client()


class TestDocConsistency:
    """第 11/18 项：README 承诺的东西必须真的存在、真的能跑。"""

    def test_readme_referenced_scripts_exist(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        for rel in ("tests/smoke_test.py", "tests/host_load_probe.py",
                    "tests/test_parser.py", "tests/test_v1019_egress.py",
                    "tests/test_v1019_hygiene.py"):
            assert rel in readme, f"README 未提及 {rel}"
            assert (PLUGIN_DIR / rel).exists(), f"README 承诺的 {rel} 不存在"

    def test_readme_does_not_claim_runnable_run_gates(self):
        """`run_gates.py` 属 devkit（scaffold 不会拷进插件目录）→ 不得声称可直接执行。"""
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        offenders = [ln for ln in readme.splitlines()
                     if ln.strip().startswith("python run_gates.py")]
        assert not offenders, f"README 声称可执行 run_gates.py（实际不存在）: {offenders}"
        assert not (PLUGIN_DIR / "run_gates.py").exists()

    def test_readme_points_at_authoritative_check_plugin(self):
        """门禁表必须指向权威副本（maibot-plugin-prep/scripts），不是 ~/maibot-dev-tools。"""
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        assert "maibot-plugin-prep/scripts/check_plugin.py" in readme


class TestConfigVersionContract:
    """SDK 只要求 **[plugin] 节** 有 config_version（不是每个分节模型）。

    依据：`maibot_sdk.config.extract_plugin_config_version` 只读
    `config_data["plugin"]["config_version"]`，缺失/为空才抛
    PluginConfigVersionError。写这条用例时曾误以为「每个分节都要有」，
    结果把正确的实现报成缺陷（第 14 项：先甄别归属再动手）。
    """

    def test_sdk_version_contract_matches_plugin_shape(self):
        from maibot_sdk.config import (
            PluginConfigVersionError,
            extract_plugin_config_version,
        )
        model = getattr(plugin_module.BilibiliVideoParserPlugin, "config_model")
        data = model().model_dump()
        # 必须能通过 SDK 的版本闸
        assert extract_plugin_config_version(data) == "1.0"
        # 反向：拿掉 [plugin].config_version 必须被拒（确认这道闸真的在生效）
        broken = {k: v for k, v in data.items() if k != "plugin"}
        with pytest.raises(PluginConfigVersionError):
            extract_plugin_config_version(broken)

    def test_only_plugin_section_needs_config_version(self):
        src = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        models = {}
        for n in ast.walk(tree):
            if not isinstance(n, ast.ClassDef):
                continue
            bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                     for b in n.bases}
            if "PluginConfigBase" in bases:
                models[n.name] = {t.target.id for t in n.body
                                  if isinstance(t, ast.AnnAssign)
                                  and isinstance(t.target, ast.Name)}
        assert models, "未找到配置模型"
        assert "config_version" in models["PluginSectionConfig"]


class TestVersionAlignment:
    def test_manifest_and_module_version_match(self):
        """manifest 版本与 PLUGIN_VERSION 错位会让 Host 装载到错版本。"""
        manifest = json.loads(
            (PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        assert manifest["version"] == plugin_module.PLUGIN_VERSION, (
            f"manifest={manifest['version']} "
            f"plugin.py={plugin_module.PLUGIN_VERSION}")

    def test_manifest_id_matches(self):
        manifest = json.loads(
            (PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        assert manifest["id"] == PLUGIN_ID


class TestInjectionGuard:
    """第 10/12 项：注入块不能是空内容或纯占位符（会安静误导 bot）。"""

    async def _resolve(self, plugin, info):
        await plugin.on_load()
        plugin._client = _stub_client(info)
        return await plugin._resolve_video(VideoTarget("BV1Sq4y167Rj"))

    async def test_normal_info_block_contains_real_facts(self, plugin):
        info = {"bvid": "BV1Sq4y167Rj", "cid": 1, "title": "某视频",
                "desc": "", "duration": 125, "owner": "某UP", "owner_mid": 1,
                "view": 12345, "danmaku": 67, "like": 89, "coin": 10,
                "favorite": 11, "pages": 1, "page": 1}
        text, level = await self._resolve(plugin, info)
        assert level == "basic"
        assert text and text.strip(), "注入块为空 —— bot 会拿到无内容上下文"
        assert "某视频" in text and "某UP" in text and "2:05" in text
        # 注意别用 lstrip()：会把要断言的前导换行一起吃掉
        assert text.startswith("\n\n[B站视频解析"), repr(text[:30])

    async def test_degenerate_info_uses_explicit_placeholder(self, plugin):
        """全空 info（异常响应）要显式写「（无）」，而不是留白。"""
        info = {"bvid": "BV1Sq4y167Rj", "cid": 0, "title": "", "desc": "",
                "duration": 0, "owner": "", "owner_mid": 0, "view": 0,
                "danmaku": 0, "like": 0, "coin": 0, "favorite": 0,
                "pages": 1, "page": 1}
        text, _level = await self._resolve(plugin, info)
        assert text and text.strip()
        assert "（无）" in text, "空字段应有显式占位，避免 bot 以为拿到了真标题"

    async def test_inject_header_appears_exactly_once(self, plugin):
        info = {"bvid": "BV1Sq4y167Rj", "cid": 1, "title": "t", "desc": "",
                "duration": 90, "owner": "u", "owner_mid": 1, "view": 1,
                "danmaku": 1, "like": 1, "coin": 1, "favorite": 1,
                "pages": 1, "page": 1}
        text, _ = await self._resolve(plugin, info)
        assert text.count(plugin_module._INJECT_HEADER) == 1

    def test_clip_enforces_hard_cap(self):
        """超长内容必须被硬截断（防上下文膨胀）。"""
        clipped = plugin_module.BilibiliVideoParserPlugin._clip("字" * 5000)
        assert len(clipped) == plugin_module._INJECT_MAX_CHARS
        assert clipped.endswith("…")

    def test_clip_is_identity_below_cap(self):
        short = "短文本"
        assert plugin_module.BilibiliVideoParserPlugin._clip(short) == short


class TestCommandPatterns:
    """命令正则必须能匹配 README 承诺的用法，且不误吃普通消息。"""

    @staticmethod
    def _pattern():
        src = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        m = re.search(r'@Command\(\s*"[^"]+",\s*pattern=r"([^"]+)"', src)
        assert m, "未找到 @Command 正则"
        return re.compile(m.group(1))

    def test_bili_command_matches_documented_forms(self):
        pat = self._pattern()
        for ok in ("/bili BV1Sq4y167Rj", "/bili av12345",
                   "/bili https://www.bilibili.com/video/BV1Sq4y167Rj",
                   "／bili b23.tv/AbCdEf9x", "  /  bili  BV1Sq4y167Rj  "):
            assert pat.match(ok), f"命令正则漏匹配: {ok!r}"

    def test_bili_command_rejects_missing_or_lookalike(self):
        pat = self._pattern()
        assert not pat.match("/bili"), "无参数也被匹配（应在 handler 里报用法）"
        assert not pat.match("/bilibili BV1Sq4y167Rj")


class TestTargetHint:
    """`_has_target_hint` 是热路径第一道门，漏判会让整条解析链永不触发。"""

    def test_hint_accepts_all_supported_forms(self):
        f = plugin_module._has_target_hint
        assert f("看看 BV1Sq4y167Rj")
        assert f("https://b23.tv/AbCdEf9x")
        assert f("https://www.bilibili.com/video/BV1Sq4y167Rj")
        assert f("av1000")

    def test_hint_rejects_plain_text(self):
        f = plugin_module._has_target_hint
        assert not f("普通聊天没有目标")
        assert not f("")
        assert not f("av12")     # 3 位以下 av 号是占位/失效号
        assert not f("BV123")    # 长度不足
