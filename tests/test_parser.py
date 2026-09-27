"""pytest 行为测试：链接提取、降级链、格式化、manifest 反向断言。

    python -m pytest tests/test_parser.py -v
"""

import asyncio
import json
import logging
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402
import plugin as plugin_module  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = "org.mai-mai.bilibili-video-parser"

from bili_video import (  # noqa: E402
    VideoTarget,
    extract_targets_from_text,
    format_count,
    format_duration,
    parse_explicit_target,
)


# ── manifest 反向断言 ─────────────────────────────────────────

def test_manifest_declares_required_capabilities():
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    caps = set(manifest["capabilities"])
    assert {"send.text", "llm.generate"} <= caps, f"能力声明缺失: {caps}"
    assert manifest["id"] == PLUGIN_ID
    assert manifest["manifest_version"] == 2


def test_manifest_dependencies_strict_fields():
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    for dep in manifest.get("dependencies", []):
        assert set(dep.keys()) <= {"type", "name", "version_spec"}, f"依赖字段越白名单: {dep}"
        if dep["type"] == "plugin":
            assert "id" in dep or "name" in dep
        else:
            assert "name" in dep and "version_spec" in dep


# ── 链接提取 ──────────────────────────────────────────────────

class TestExtract:
    def test_bv_bare(self):
        assert extract_targets_from_text("看看 BV1xx411c7XZ 这个") == [("BV1xx411c7XZ", "bv")]

    def test_av_bare(self):
        assert extract_targets_from_text("av170001 经典") == [("av170001", "av")]

    def test_full_url_with_page(self):
        hits = extract_targets_from_text(
            "https://www.bilibili.com/video/BV1xx411c7XZ?p=3 好活")
        assert hits == [("BV1xx411c7XZ?p=3", "url")]

    def test_b23_short_link(self):
        hits = extract_targets_from_text("https://b23.tv/AbCdEf 看这个")
        assert hits == [("AbCdEf", "b23")]

    def test_dedup_same_bv(self):
        text = "BV1xx411c7XZ 和 https://www.bilibili.com/video/BV1xx411c7XZ 同一个"
        hits = extract_targets_from_text(text)
        assert len(hits) == 1

    def test_no_false_positive(self):
        assert extract_targets_from_text("今天天气不错，出去走走") == []
        assert extract_targets_from_text("av 123456") == []  # av 后必须直接跟数字
        assert extract_targets_from_text("BV123") == []  # BV 长度不足

    def test_explicit_target_bv(self):
        t = parse_explicit_target("/bili BV1xx411c7XZ")
        assert t.video_id == "BV1xx411c7XZ" and t.page == 1

    def test_explicit_target_url_page(self):
        t = parse_explicit_target("https://www.bilibili.com/video/BV1xx411c7XZ?p=2")
        assert t.video_id == "BV1xx411c7XZ" and t.page == 2

    def test_explicit_target_b23(self):
        t = parse_explicit_target("https://b23.tv/AbCdEf")
        assert t.video_id == "b23:AbCdEf"

    def test_explicit_target_invalid(self):
        assert parse_explicit_target("随便打点字") is None
        assert parse_explicit_target("") is None


# ── 格式化 ────────────────────────────────────────────────────

class TestFormat:
    def test_count(self):
        assert format_count(999) == "999"
        assert format_count(12345) == "1.2万"
        assert format_count(234_000_000) == "2.3亿"

    def test_duration(self):
        assert format_duration(59) == "0:59"
        assert format_duration(125) == "2:05"
        assert format_duration(3671) == "1:01:11"


# ── 降级链（mock 客户端） ─────────────────────────────────────

class FakeBiliClient:
    """可控的 B 站客户端替身：按测试需要返回各层数据。"""

    def __init__(self, info=None, conclusion=None, subtitle=None, shot=None,
                 resolve_error=None):
        self.info = info or _sample_info()
        self.conclusion = conclusion
        self.subtitle = subtitle
        self.shot = shot
        self.resolve_error = resolve_error
        self.resolved: list[str] = []

    async def resolve_target(self, target):
        self.resolved.append(target.video_id)
        if self.resolve_error:
            raise self.resolve_error
        return target

    async def get_video_info(self, target):
        return dict(self.info)

    async def get_ai_conclusion(self, info):
        return self.conclusion

    async def get_subtitle(self, info):
        return self.subtitle

    async def get_videoshot(self, info):
        return self.shot

    async def download_image(self, url):
        return None

    async def close(self):
        pass


def _sample_info():
    return {
        "bvid": "BV1xx411c7XZ", "aid": 170001, "cid": 2799, "title": "测试视频",
        "desc": "这是简介", "duration": 613, "owner": "测试UP", "owner_mid": 1,
        "view": 1234567, "danmaku": 4567, "reply": 890, "like": 98765,
        "coin": 4321, "favorite": 21000, "pages": 1, "page": 1,
    }


@pytest.fixture()
def plugin():
    module = load_plugin_module(PLUGIN_DIR)
    p = module.create_plugin()
    host = FakeHost(plugin_id=PLUGIN_ID)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    bind_context(p, ctx, get_default_config(getattr(type(p), "config_model", None)))
    return p


class TestDegradationChain:
    async def test_l1_ai_summary_hit(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "官方总结内容", "outline": [
                {"title": "第一章", "part_outline": []}]})
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "ai_summary"
        assert "官方总结内容" in text
        assert "第一章" in text
        assert "测试UP" in text  # 基础信息始终在场
        await plugin.on_unload()

    async def test_l2_frame_vision_hit(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient(shot={"image_urls": ["http://x/i.jpg"],
                                              "img_x_len": 10, "img_y_len": 10,
                                              "img_x_size": 160, "img_y_size": 90})

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                return "关键帧总结文本"

        plugin._vision = FakeVision()
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "frame_vision"
        assert "关键帧总结文本" in text
        await plugin.on_unload()

    async def test_l2_subtitle_hit(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient(subtitle="字幕正文内容很多字" * 200)
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "subtitle"
        assert "字幕节选" in text
        # 字幕裁剪生效
        assert len(text) <= 1200
        await plugin.on_unload()

    async def test_l2_desc_fallback(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient()
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "desc"
        assert "[简介]" in text and "这是简介" in text
        await plugin.on_unload()

    async def test_l3_basic_when_no_desc(self, plugin):
        await plugin.on_load()
        info = _sample_info()
        info["desc"] = "-"
        plugin._client = FakeBiliClient(info=info)
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "basic"
        assert "测试视频" in text and "测试UP" in text
        await plugin.on_unload()

    async def test_cache_reuse(self, plugin):
        await plugin.on_load()
        client = FakeBiliClient(
            conclusion={"summary": "缓存验证", "outline": []})
        plugin._client = client
        t1, l1 = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        t2, l2 = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert l1 == "ai_summary" and l2 == "cache"
        assert t1 == t2
        assert len(client.resolved) == 1  # 第二次没打网络
        await plugin.on_unload()

    async def test_short_video_skips_frame_vision(self, plugin):
        await plugin.on_load()
        info = _sample_info()
        info["duration"] = 30  # 短于 min_video_duration_sec
        plugin._client = FakeBiliClient(
            info=info, shot={"image_urls": ["http://x/i.jpg"], "img_x_len": 10,
                             "img_y_len": 10, "img_x_size": 160, "img_y_size": 90})

        called = {"vision": False}

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                called["vision"] = True
                return "不应走到这里"

        plugin._vision = FakeVision()
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "desc"
        assert called["vision"] is False
        await plugin.on_unload()


# ── hook 行为 ─────────────────────────────────────────────────

class TestHook:
    async def test_hook_injects_context(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "hook注入验证", "outline": []})
        message = {
            "message_id": "m1",
            "processed_plain_text": "看看这个 https://www.bilibili.com/video/BV1xx411c7XZ",
            "text": "看看这个 https://www.bilibili.com/video/BV1xx411c7XZ",
            "message_info": {"group_id": 123},
        }
        kwargs = {"message": message}
        result = await plugin.on_incoming_message(**kwargs)
        assert result["action"] == "continue"
        # 改写的是同一个 message 对象（Host 按 kwargs["message"] 引用取回）
        assert result.get("modified_kwargs", {}).get("message") is message
        assert "[B站视频解析" in message["processed_plain_text"]
        assert "hook注入验证" in message["processed_plain_text"]
        await plugin.on_unload()

    async def test_hook_dedup_same_message(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "去重验证", "outline": []})
        message = {
            "message_id": "m2",
            "processed_plain_text": "BV1xx411c7XZ",
            "message_info": {},
        }
        r1 = await plugin.on_incoming_message(message=message)
        before = message["processed_plain_text"]
        r2 = await plugin.on_incoming_message(message=message)
        assert "[B站视频解析" in before
        # 第二次同 message_id 不再注入（文本没变）
        assert message["processed_plain_text"] == before
        assert r2.get("modified_kwargs") is None
        await plugin.on_unload()

    async def test_hook_ignores_plain_text(self, plugin):
        await plugin.on_load()
        message = {
            "message_id": "m3",
            "processed_plain_text": "普通聊天消息",
            "message_info": {},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result == {"action": "continue"}
        assert message["processed_plain_text"] == "普通聊天消息"
        await plugin.on_unload()

    async def test_hook_group_disabled(self, plugin):
        await plugin.on_load()
        plugin.config.parse.enable_in_group = False
        message = {
            "message_id": "m4",
            "processed_plain_text": "BV1xx411c7XZ",
            "message_info": {"group_id": 999},
        }
        result = await plugin.on_incoming_message(message=message, is_group=True)
        assert result == {"action": "continue"}
        await plugin.on_unload()

    async def test_hook_parse_failure_still_injects_marker(self, plugin):
        await plugin.on_load()
        plugin._client = FakeBiliClient(resolve_error=ValueError("视频不存在"))
        message = {
            "message_id": "m5",
            "processed_plain_text": "BV1xx411c7XZ",
            "message_info": {},
        }
        await plugin.on_incoming_message(message=message)
        assert "解析失败" in message["processed_plain_text"]
        # 异常脱敏：不回显异常详情（ValueError 文本不得出现在注入里）
        assert "视频不存在" not in message["processed_plain_text"]
        await plugin.on_unload()


# ── v1.0.1 修复项回归 ─────────────────────────────────────────

class TestV101Fixes:
    async def test_b23_bare_code_in_hook(self, plugin):
        """b23 裸短码在 hook 链路应被解析（v1.0.0 中被静默丢弃）。"""
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "b23短码注入验证", "outline": []})
        message = {
            "message_id": "b23-1",
            "processed_plain_text": "看这个 https://b23.tv/AbCdEf9x",
            "message_info": {},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert "b23短码注入验证" in message["processed_plain_text"]
        await plugin.on_unload()

    async def test_b23_cache_hit_after_resolve(self, plugin):
        """同一 b23 短链第二次解析应命中缓存（resolve 后按 real_target 回查）。"""
        await plugin.on_load()
        client = FakeBiliClient(conclusion={"summary": "b23缓存验证", "outline": []})
        plugin._client = client
        t1, l1 = await plugin._resolve_video(VideoTarget("b23:AbCdEf9x"))
        t2, l2 = await plugin._resolve_video(VideoTarget("b23:AbCdEf9x"))
        assert l1 == "ai_summary" and l2 == "cache"
        assert t1 == t2
        # 短链解析只发生一次
        assert client.resolved.count("b23:AbCdEf9x") == 1
        await plugin.on_unload()

    async def test_hook_light_path_skips_frame_vision(self, plugin):
        """hook 轻量路径不跑关键帧（v1.0.8 起关键帧转后台补发，注入路径永远跳过）。"""
        await plugin.on_load()
        assert plugin.config.trigger.enable_frame_vision_in_hook is True  # 后台补发开关默认开
        called = {"vision": False}

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                called["vision"] = True
                return "不应走到这里"

        plugin._vision = FakeVision()
        plugin._client = FakeBiliClient(
            shot={"image_urls": ["http://x/i.jpg"], "img_x_len": 10,
                  "img_y_len": 10, "img_x_size": 160, "img_y_size": 90})
        message = {
            "message_id": "fv-1",
            "processed_plain_text": "BV1xx411c7XZ",
            "message_info": {},
        }
        await plugin.on_incoming_message(message=message)
        assert called["vision"] is False
        # 应降级到简介而不是关键帧
        assert "[简介]" in message["processed_plain_text"]
        await plugin.on_unload()

    async def test_command_path_allows_frame_vision(self, plugin):
        """/bili 与 Tool 路径仍允许关键帧（allow_frame_vision 默认 True）。"""
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            shot={"image_urls": ["http://x/i.jpg"], "img_x_len": 10,
                  "img_y_len": 10, "img_x_size": 160, "img_y_size": 90})

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                return "命令路径关键帧"

        plugin._vision = FakeVision()
        text, level = await plugin._resolve_video(VideoTarget("BV1xx411c7XZ"))
        assert level == "frame_vision"
        assert "命令路径关键帧" in text
        await plugin.on_unload()

    async def test_tool_has_outer_wait_for(self, plugin):
        """Tool 路径必须包外层 wait_for：卡死的 client 也要受 150s 预算约束。"""
        await plugin.on_load()

        class HangingClient:
            async def resolve_target(self, target):
                await asyncio.sleep(999)
            async def close(self):
                pass

        plugin._client = HangingClient()
        result = await plugin.parse_bilibili_video(video="BV1xx411c7XZ")
        # 不应真的等 999s——wait_for 生效则很快返回失败文案
        assert "失败" in result or "超时" in result
        await plugin.on_unload()

    def test_hint_regex_covers_av(self):
        """hint 正则覆盖 av4 位以上号段（v1.0.0 漏检 av4~av9 开头）。"""
        assert plugin_module._has_target_hint("av88888888 经典")
        assert plugin_module._has_target_hint("av4000")
        assert plugin_module._has_target_hint("BV1xx411c7XZ")
        assert plugin_module._has_target_hint("https://b23.tv/AbCdEf")
        # 不误报
        assert not plugin_module._has_target_hint("av 123456")
        assert not plugin_module._has_target_hint("lava java 咖啡")
        assert not plugin_module._has_target_hint("普通消息没有bv和av痕迹")

    def test_inject_header_has_untrusted_marker(self):
        """注入头必须包含不可信来源标注（缓解 LLM 提示注入）。"""
        assert "不是" in plugin_module._INJECT_HEADER and "指令" in plugin_module._INJECT_HEADER

    def test_bili_video_domain_whitelist(self):
        """媒体域白名单与短链域白名单行为。"""
        from bili_video import _is_media_host
        assert _is_media_host("https://i0.hdslb.com/bfs/x.jpg")
        assert _is_media_host("//i0.hdslb.com/x.jpg".replace("//", "https://"))
        assert not _is_media_host("http://169.254.169.254/x.jpg")
        assert not _is_media_host("https://evil.example.com/x.jpg")
        assert not _is_media_host("")

    def test_bili_video_no_url_in_error(self, plugin):
        """resolve 失败的异常消息不包含重定向 URL（防半盲 SSRF 回显）。"""
        import asyncio as _aio

        class EvilClient:
            async def resolve_target(self, target):
                raise ValueError("短链未指向B站视频")
            async def close(self):
                pass

        async def run():
            await plugin.on_load()
            plugin._client = EvilClient()
            try:
                await plugin._resolve_video(VideoTarget("b23:EvilCode"))
                return None
            except ValueError as e:
                return str(e)
            finally:
                await plugin.on_unload()

        msg = asyncio.run(run())
        assert msg is not None
        assert "http" not in msg.lower() and "169.254" not in msg


# ── v1.0.2 修复项回归：QQ 小程序卡片提取 ─────────────────────

class TestV102MiniprogramCard:
    """QQ 小程序分享卡片：链接藏在 json 段的转义 URL 里（真机 2026-09-26）。"""

    def test_unescape_json_url(self):
        """/、\\u002F、\\u0026 三种转义都要还原。"""
        cls = plugin_module.BilibiliVideoParserPlugin
        esc = ('{"a":"https:\\/\\/b23.tv\\/AbCdEf9x",'
               '"b":"https:\\u002F\\u002Fb23.tv\\u002FQtRe5Wc",'
               '"c":"\\u0026nbsp"}')
        out = cls._unescape_json_url(esc)
        assert "https://b23.tv/AbCdEf9x" in out
        assert "https://b23.tv/QtRe5Wc" in out
        assert "&nbsp" in out

    async def test_hook_extracts_b23_from_json_segment(self, plugin):
        """json 段（list raw_message）里的转义 b23 链接应触发解析注入。"""
        import json as _json
        ark = _json.dumps({
            "meta": {"detail_1": {"qqdocurl": "https://b23.tv/AbCdEf9x"}}
        }).replace("/", "\\/")  # 模拟 QQ 卡片的 \/ 转义形态
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "小程序卡片注入验证", "outline": []})
        message = {
            "message_id": "mp-card-1",
            "processed_plain_text": "[小程序] 哔哩哔哩：某视频标题",
            "raw_message": [{"type": "json", "data": {"data": ark}}],
            "message_info": {},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert "小程序卡片注入验证" in message["processed_plain_text"]
        await plugin.on_unload()

    async def test_hook_no_target_card_falls_to_title_search(self, plugin):
        """小程序卡片无链接目标：走标题反查兜底（v1.0.3），反查失败则静默。"""
        await plugin.on_load()
        logs: list[str] = []

        class LogCap:
            def info(self, msg, *a, **k):
                logs.append(str(msg))
            def warning(self, msg, *a, **k):
                logs.append(str(msg))

        # PluginContext 的 ctx/logger 均为只读 property，手动替换类级 logger property
        ctx_type = type(plugin.ctx)
        sentinel = object()
        original_prop = ctx_type.__dict__.get("logger", sentinel)
        setattr(ctx_type, "logger", property(lambda self: LogCap()))

        class SearchNoneClient(FakeBiliClient):
            searched: list = []

            async def search_by_title(self, kw):
                SearchNoneClient.searched.append(kw)
                return None

        plugin._client = SearchNoneClient()
        original = "[小程序] 哔哩哔哩：无链接卡片"
        try:
            message = {
                "message_id": "mp-card-2",
                "processed_plain_text": original,
                "raw_message": [{"type": "json", "data": {"data": "{}"}}],
                "message_info": {},
            }
            result = await plugin.on_incoming_message(message=message)
            assert result["action"] == "continue"
            assert message["processed_plain_text"] == original
            # 标题反查被触发且无结果
            assert SearchNoneClient.searched == ["无链接卡片"]
            assert any("反查无结果" in x for x in logs)
        finally:
            if original_prop is not sentinel:
                setattr(ctx_type, "logger", original_prop)
            await plugin.on_unload()


# ── v1.0.3 修复项回归：小程序卡片标题反查 ─────────────────────

class TestV103TitleSearch:
    """json 载荷被管线剥掉后（真机 16:58 实证 raw 只剩 text 段），
    用卡片标题反查 B 站搜索兜底。"""

    def test_extract_card_title(self):
        cls = plugin_module.BilibiliVideoParserPlugin
        t = cls._extract_card_title(
            "[小程序] 哔哩哔哩：【洛天依原创】杀人兔 - QGRay [图片：截图] 图片[未识别]")
        assert t == "【洛天依原创】杀人兔 - QGRay"
        # 非 B 站小程序 / 过短标题
        assert cls._extract_card_title("[小程序] 某App：标题") == ""
        assert cls._extract_card_title("[小程序] 哔哩哔哩：短") == ""

    async def test_hook_falls_back_to_title_search(self, plugin):
        """无链接卡片 → 标题反查命中 → 正常注入解析结果。"""
        await plugin.on_load()

        class SearchClient(FakeBiliClient):
            async def search_by_title(self, kw):
                assert "杀人兔" in kw
                return VideoTarget("BV1xx411c7XZ")

        plugin._client = SearchClient(
            conclusion={"summary": "标题反查注入验证", "outline": []})
        message = {
            "message_id": "mp-card-title-1",
            "processed_plain_text":
                "[小程序] 哔哩哔哩：【洛天依原创】杀人兔 - QGRay",
            "raw_message": [{"type": "text", "data": {"text": "[小程序]"}}],
            "message_info": {},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert "标题反查注入验证" in message["processed_plain_text"]
        await plugin.on_unload()

    async def test_hook_title_search_no_result_silent(self, plugin):
        """反查无结果：不注入不报错（消息保持原样）。"""
        await plugin.on_load()

        class SearchNoneClient(FakeBiliClient):
            async def search_by_title(self, kw):
                return None

        plugin._client = SearchNoneClient()
        original = "[小程序] 哔哩哔哩：不存在的视频标题"
        message = {
            "message_id": "mp-card-title-2",
            "processed_plain_text": original,
            "raw_message": [],
            "message_info": {},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert message["processed_plain_text"] == original
        await plugin.on_unload()


# ── v1.0.4 修复项回归：NapCat get_msg 回查（上游 Maisaka 版同款方案） ──

class TestV104NapcatGetMsg:
    """MaiBot 消息体丢 json 载荷时，调适配器 get_msg API 回查原始消息。"""

    def test_collect_candidate_texts_parses_nested_json(self):
        """深度扫描：字符串内嵌 JSON 要被解析展开；优先键先扫。"""
        cls = plugin_module.BilibiliVideoParserPlugin
        import json as _json
        ark = _json.dumps({
            "meta": {"detail_1": {
                "qqdocurl": "https://b23.tv/AbCdEf9x",
                "title": "某视频"}},
            "prompt": "哔哩哔哩：某视频",
        }).replace("/", "\\/")  # 模拟 QQ 转义形态
        detail = {"message": [{"type": "json", "data": {"data": ark}}]}
        candidates: list = []
        cls._collect_candidate_texts(detail, candidates, seen=set())
        joined = "\n".join(candidates)
        assert "b23.tv/AbCdEf9x" in joined
        # 还原转义后可提取
        hits = plugin_module.extract_targets_from_text(
            plugin_module.BilibiliVideoParserPlugin._unescape_json_url(joined))
        assert ("AbCdEf9x", "b23") in hits

    async def test_hook_get_msg_channel_injects(self, plugin):
        """卡片无链接 + get_msg 通道可用 → 回查命中 → 注入。"""
        import json as _json
        import types as _types

        ark = _json.dumps({
            "meta": {"detail_1": {"qqdocurl": "https://b23.tv/AbCdEf9x"}}
        }).replace("/", "\\/")

        class FakeAPI:
            async def call(self, api_name, version="", **kwargs):
                assert api_name == "adapter.napcat.message.get_msg"
                assert kwargs.get("message_id") == "mp-card-gm-1"
                return {
                    "message_id": "mp-card-gm-1",
                    "message": [{"type": "json",
                                 "data": {"data": ark}}],
                }

        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "get_msg回查注入验证", "outline": []})
        # SDK 2.8.1 无 ctx.api，测试里挂一个假代理
        plugin.ctx.api = _types.SimpleNamespace(call=FakeAPI().call)
        try:
            message = {
                "message_id": "mp-card-gm-1",
                "processed_plain_text":
                    "[小程序] 哔哩哔哩：【洛天依原创】杀人兔 - QGRay",
                "raw_message": [{"type": "text",
                                 "data": {"text": "[小程序]"}}],
                "message_info": {},
            }
            result = await plugin.on_incoming_message(message=message)
            assert result["action"] == "continue"
            assert "get_msg回查注入验证" in message["processed_plain_text"]
        finally:
            await plugin.on_unload()

    async def test_hook_get_msg_unavailable_falls_to_title_search(self, plugin):
        """get_msg 通道不可用（api.call 抛错）→ 回退标题反查。"""
        import types as _types

        class BrokenAPI:
            async def call(self, *a, **k):
                raise PermissionError("不允许调用")

        class SearchClient(FakeBiliClient):
            async def search_by_title(self, kw):
                assert "杀人兔" in kw
                return VideoTarget("BV1xx411c7XZ")

        await plugin.on_load()
        plugin._client = SearchClient(
            conclusion={"summary": "回退反查注入验证", "outline": []})
        plugin.ctx.api = _types.SimpleNamespace(call=BrokenAPI().call)
        try:
            message = {
                "message_id": "mp-card-gm-2",
                "processed_plain_text":
                    "[小程序] 哔哩哔哩：【洛天依原创】杀人兔 - QGRay",
                "raw_message": [],
                "message_info": {},
            }
            result = await plugin.on_incoming_message(message=message)
            assert result["action"] == "continue"
            assert "回退反查注入验证" in message["processed_plain_text"]
        finally:
            await plugin.on_unload()

    async def test_looks_like_bili_card(self):
        cls = plugin_module.BilibiliVideoParserPlugin
        assert cls._looks_like_bili_card("[小程序] 哔哩哔哩：标题")
        assert cls._looks_like_bili_card("[json] {...}")
        assert cls._looks_like_bili_card("哔哩哔哩：无链接标题文本")
        # 有链接/ID 的普通消息不进卡片兜底
        assert not cls._looks_like_bili_card("看这个 BV1xx411c7XZ")
        assert not cls._looks_like_bili_card("今天天气不错")


# ── v1.0.6 修复项回归：注入幂等守卫（1.3.0 双 hook） ──────────

class TestV106InjectIdempotency:
    """MaiBot 1.3.0 对同消息触发两次 hook 且 message_id 去重拦不住，
    注入块重复两遍（真机 13:00 实测）。注入前做内容级幂等检查。"""

    async def test_second_hook_call_does_not_duplicate(self, plugin):
        """正文已含相同注入块时，第二次调用不再追加。"""
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "幂等验证总结", "outline": []})
        link_text = "看这个 https://b23.tv/AbCdEf9x"
        message = {
            "message_id": "idem-1",
            "processed_plain_text": link_text,
            "message_info": {},
        }
        r1 = await plugin.on_incoming_message(message=message)
        assert r1["action"] == "continue"
        first = message["processed_plain_text"]
        assert first.count("幂等验证总结") == 1
        # 模拟 1.3.0 双 hook：不同 message_id、正文已含注入块
        message2 = {
            "message_id": "idem-2",
            "processed_plain_text": first,
            "message_info": {},
        }
        r2 = await plugin.on_incoming_message(message=message2)
        assert r2["action"] == "continue"
        assert message2["processed_plain_text"].count("幂等验证总结") == 1
        await plugin.on_unload()

    async def test_different_videos_still_both_inject(self, plugin):
        """幂等守卫不误伤：同一消息里两个不同视频各自注入。"""
        await plugin.on_load()

        class MultiClient(FakeBiliClient):
            async def get_video_info(self, target):
                info = await super().get_video_info(target)
                info = dict(info)
                info["title"] = f"视频{target.video_id}"
                return info

        plugin._client = MultiClient(
            conclusion={"summary": "多视频注入", "outline": []})
        message = {
            "message_id": "idem-3",
            "processed_plain_text":
                "BV1xx411c7XZ 和 BV1yy411c7YY 都看看",
            "message_info": {},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        text = message["processed_plain_text"]
        assert text.count("多视频注入") == 2  # 两个视频各一次
        await plugin.on_unload()


# ── v1.0.7 修复项回归：L1 conclusion 接口（真机 12:59 配 SESSDATA 仍降级） ──

class TestV107Conclusion:
    """view/conclusion/get 需 WBI 签名 + SESSDATA；旧代码路径少了 /get
    且未签名，L1 从上线起就静默失败。"""

    @staticmethod
    def _make_client(payload=None):
        """直接测真 BiliVideoClient（FakeBiliClient 的 get_ai_conclusion
        是 mock，覆盖了真实现），捕获 _get_json 调用。"""
        from bili_video import BiliVideoClient

        captured = {}

        class CapClient(BiliVideoClient):
            async def _ensure_mixin_key(self):
                return "d4c4a1b2c3d4e5f6a7b8c9d0e1f2a3b4"

            async def _get_json(self, url, params=None, extra_headers=None):
                captured["url"] = url
                captured["params"] = dict(params or {})
                return payload

        return CapClient(), captured

    async def test_conclusion_uses_signed_get_url(self):
        """断言请求打到 /view/conclusion/get 且带 w_rid 签名。"""
        client, captured = self._make_client({
            "code": 0,
            "data": {
                "code": 0,
                "model_result": {
                    "summary": "官方总结内容",
                    "outline": [{"title": "开场"}],
                },
            },
        })
        result = await client.get_ai_conclusion(
            {"bvid": "BV1Sq4y167Rj", "cid": 12345, "owner_mid": 678})
        assert result == {"summary": "官方总结内容",
                          "outline": [{"title": "开场"}]}
        assert captured["url"].endswith("/x/web-interface/view/conclusion/get")
        assert captured["params"]["w_rid"]          # 已签名
        assert captured["params"]["wts"]
        assert captured["params"]["bvid"] == "BV1Sq4y167Rj"
        assert captured["params"]["cid"] == "12345"  # _wbi_sign 统一转 str
        assert captured["params"]["up_mid"] == "678"

    async def test_conclusion_records_failure_reasons(self):
        """接口失败/无摘要时记录可读原因（排障观测）。"""
        client, _ = self._make_client({"code": -403, "message": "访问权限不足"})
        r = await client.get_ai_conclusion(
            {"bvid": "BV1Sq4y167Rj", "cid": 1, "owner_mid": 2})
        assert r is None
        assert "-403" in client.last_conclusion_error

        client2, _ = self._make_client(
            {"code": 0, "data": {"code": 1, "model_result": {}}})
        r = await client2.get_ai_conclusion(
            {"bvid": "BV1Sq4y167Rj", "cid": 1, "owner_mid": 2})
        assert r is None
        assert "data.code=1" in client2.last_conclusion_error


# ── v1.0.8 修复项回归：关键帧后台补发 ─────────────────────────

class TestV108BackgroundFrameVision:
    """hook 轻量注入后，关键帧识别+宿主总结转后台任务。

    v1.0.12 起后果改为**入待注入队列**（下一轮模型请求注入上下文），
    不再单独发一条消息。"""

    def _make_plugin(self, plugin, api_calls: list | None = None,
                     vision_result="关键帧看到的内容"):
        """给 plugin 挂 mock client/vision/api 代理。"""
        import types as _types
        api_calls = api_calls if api_calls is not None else []

        class BgClient(FakeBiliClient):
            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "某长视频",
                        "duration": 300, "owner_mid": 5,
                        "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

        plugin._client = BgClient()
        vision_called = {"n": 0}

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                vision_called["n"] += 1
                return vision_result

        plugin._vision = FakeVision()

        class FakeAPI:
            async def call(self, api_name, version="", **kwargs):
                api_calls.append((api_name, kwargs))
                return {"status": "ok", "retcode": 0, "data": {}}

        plugin.ctx.api = _types.SimpleNamespace(call=FakeAPI().call)
        return vision_called

    @staticmethod
    def _msg(group_id=None, user_id=456, session_id="stream-1"):
        gi = {"group_id": group_id} if group_id else {}
        return {"session_id": session_id,
                "message_info": {"group_info": gi,
                                 "user_info": {"user_id": user_id}}}

    async def test_bg_task_queues_summary_and_sends_nothing(self, plugin):
        """后台任务完成后只入队，不发任何消息（v1.0.12 形态变更）。"""
        host = FakeHost(plugin_id=PLUGIN_ID)
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        await plugin.on_load()
        self._make_plugin(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg(group_id=123))
        # 入队
        entries = plugin._bg_pending.get("stream-1") or []
        assert len(entries) == 1
        assert "关键帧看到的内容" in entries[0]["text"]
        assert entries[0]["video_id"] == "BV1xx411c7XZ"
        # 一条消息都没发
        assert host.calls_of("send.text") == []
        assert host.calls_of("api.call") == []
        await plugin.on_unload()

    async def test_spawn_throttles_repeat_videos(self, plugin):
        """同视频 30 分钟内只孵化一次后台任务。"""
        await plugin.on_load()
        self._make_plugin(plugin)
        message = self._msg(group_id=1)
        target = VideoTarget("BV1xx411c7XZ")
        plugin._spawn_background_frame_vision(target, message)
        n_tasks = len(plugin._bg_tasks)
        assert n_tasks == 1
        # 第二次孵化被节流
        plugin._spawn_background_frame_vision(target, message)
        assert len(plugin._bg_tasks) == n_tasks
        await plugin.on_unload()

    async def test_hook_spawns_background_after_inject(self, plugin):
        """hook 注入成功后自动孵化后台任务，跑完进入待注入队列（端到端）。"""
        await plugin.on_load()
        self._make_plugin(plugin)
        message = {
            "message_id": "bg-1",
            "session_id": "stream-e2e",
            "processed_plain_text": "看这个 https://b23.tv/AbCdEf9x",
            "message_info": {"group_info": {"group_id": 123},
                             "user_info": {"user_id": 456}},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert len(plugin._bg_tasks) == 1
        # 等后台任务跑完（测试内立即 await）
        for t in list(plugin._bg_tasks):
            await t
        entries = plugin._bg_pending.get("stream-e2e") or []
        assert len(entries) == 1
        assert "关键帧看到的内容" in entries[0]["text"]
        await plugin.on_unload()


# ── v1.0.9 修复项回归：b23 去重 / 后台 resolve / 时间窗 ───────

class TestV109DedupAndBgResolve:
    """真机 13:52 三连：1.3.0 卡片链接在 processed+raw 各出现一次
    → b23 未去重 → 同块注入两遍；后台任务用原始 b23 target 调 view → -400。"""

    def test_extract_b23_dedup(self):
        """同一 b23 短码在拼接文本出现两次只提取一次。"""
        text = ("链接: https://b23.tv/7iolO6W[image]\n"
                "链接: https://b23.tv/7iolO6W")
        hits = plugin_module.extract_targets_from_text(text)
        b23_hits = [h for h in hits if h[1] == "b23"]
        assert len(b23_hits) == 1
        assert b23_hits[0][0] == "7iolO6W"

    async def test_bg_task_resolves_b23_first(self, plugin):
        """后台任务对 b23 目标先 resolve 再取信息（否则 view -400）。"""
        await plugin.on_load()
        # 复用 TestV108 的 mock 组装
        TestV108BackgroundFrameVision()._make_plugin(plugin)
        resolve_calls: list = []

        class ResolvingClient(FakeBiliClient):
            async def resolve_target(self, target):
                resolve_calls.append(target.video_id)
                return VideoTarget("BV1LXwyzkEqo")

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

            async def get_video_info(self, tgt):
                assert tgt.video_id == "BV1LXwyzkEqo"  # 必须是解析后的 BV
                return {"bvid": "BV1LXwyzkEqo", "cid": 1, "title": "T",
                        "duration": 300, "owner_mid": 5, "desc": "",
                        "owner": "UP", "view": 1, "danmaku": 0, "like": 0,
                        "coin": 0, "pages": 1, "page": 1}

        plugin._client = ResolvingClient()
        message = {"session_id": "stream-resolve",
                   "message_info": {"group_info": {"group_id": 1},
                                    "user_info": {}}}
        await plugin._bg_frame_vision_task(VideoTarget("b23:7iolO6W"), message)
        assert resolve_calls == ["b23:7iolO6W"]
        entries = plugin._bg_pending.get("stream-resolve") or []
        assert entries and "关键帧看到的内容" in entries[0]["text"]
        await plugin.on_unload()

    async def test_time_window_dedup_blocks_reinject(self, plugin):
        """5 分钟内同视频第二次 hook 调用不重复注入（防双 hook 副本）。"""
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "时间窗验证", "outline": []})
        text = "https://b23.tv/AbCdEf9x"
        m1 = {"message_id": "tw-1", "processed_plain_text": text,
              "message_info": {}}
        await plugin.on_incoming_message(message=m1)
        assert m1["processed_plain_text"].count("时间窗验证") == 1
        # 不同 message_id（模拟 1.3.0 双 hook 副本）
        m2 = {"message_id": "tw-2", "processed_plain_text": text,
              "message_info": {}}
        await plugin.on_incoming_message(message=m2)
        assert m2["processed_plain_text"].count("时间窗验证") == 0
        await plugin.on_unload()


# ── v1.0.10：_extract_text 形态兼容 + L1 命中跳过后台关键帧 ───

class TestV110ExtractAndSkip:
    def test_extract_text_shape_compat(self):
        """宿主响应形态兼容：平铺/包装/choices/嵌套 dict/纯字符串。"""
        ex = plugin_module.FrameVisionManager._extract_text
        assert ex({"text": "A"}) == "A"
        assert ex({"success": True, "result": {"text": "B"}}) == "B"
        assert ex({"success": True, "result": "C"}) == "C"
        assert ex({"choices": [{"message": {"content": "D"}}]}) == "D"
        assert ex({"data": {"content": "E"}}) == "E"
        assert ex("F") == "F"
        assert ex({}) is None
        assert ex({"text": "   "}) is None

    async def test_l1_hit_skips_background_frame_vision(self, plugin):
        """L1 官方总结命中时不孵化后台关键帧任务（内容已够丰富）。"""
        await plugin.on_load()
        plugin._client = FakeBiliClient(
            conclusion={"summary": "L1命中验证", "outline": []})
        message = {
            "message_id": "l1-skip-1",
            "processed_plain_text": "https://b23.tv/AbCdEf9x",
            "message_info": {"group_info": {"group_id": 1}, "user_info": {}},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert "L1命中验证" in message["processed_plain_text"]
        assert len(plugin._bg_tasks) == 0  # L1 命中 → 不孵化
        await plugin.on_unload()

    async def test_l2_still_spawns_background(self, plugin):
        """降级到 L2/L3 时仍孵化后台关键帧（回归保护）。"""
        await plugin.on_load()
        plugin._client = FakeBiliClient()  # 无 conclusion → 降级 L2c 简介
        message = {
            "message_id": "l2-spawn-1",
            "processed_plain_text": "https://b23.tv/AbCdEf9x",
            "message_info": {"group_info": {"group_id": 1}, "user_info": {}},
        }
        result = await plugin.on_incoming_message(message=message)
        assert result["action"] == "continue"
        assert len(plugin._bg_tasks) == 1
        await plugin.on_unload()




# ── v1.0.12 形态变更：关键帧总结注入上下文（不发消息） ─────────────

class TestV112ContextInject:
    """真机 15:27：补发一条 `[B站关键帧总结·…]` 独立消息在聊天流里突兀
    （用户明确要求改为"注入到上下文"）。

    新形态：后台识别完成后**只入待注入队列**；在随后的
    `maisaka.planner.before_request` / `maisaka.replyer.before_model_request`
    上把总结并入本次模型请求（prompt / messages / items 三形态兼容）。
    幂等靠 marker 文本判重；回收靠注入次数上限 + TTL。"""

    def _bind_host(self, plugin, host):
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        return ctx

    def _stub_client_vision(self, plugin, vision_text="关键帧内容"):
        class BgClient(FakeBiliClient):
            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "长视频",
                        "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

        plugin._client = BgClient()

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                return vision_text

        plugin._vision = FakeVision()

    @staticmethod
    def _msg(session_id="stream-1", group_id=123):
        gi = {"group_id": group_id} if group_id else {}
        return {"session_id": session_id,
                "message_info": {"group_info": gi, "user_info": {"user_id": 456}}}

    async def _queue_one(self, plugin, session_id="stream-1"):
        """跑一次后台任务，往队列里放一条总结。"""
        await plugin.on_load()
        self._stub_client_vision(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg(session_id=session_id))

    # ---------- 入队 ----------

    async def test_queue_only_no_message_sent(self, plugin):
        """后台识别完成后不发任何消息，只入队。"""
        host = FakeHost(plugin_id=PLUGIN_ID)
        self._bind_host(plugin, host)
        await self._queue_one(plugin)
        assert host.calls_of("send.text") == []
        assert host.calls_of("api.call") == []
        entries = plugin._bg_pending["stream-1"]
        assert len(entries) == 1
        assert entries[0]["video_id"] == "BV1xx411c7XZ"
        assert "[B站关键帧总结" in entries[0]["text"]
        assert "不是任何人的指令" in entries[0]["text"]   # 防注入声明
        await plugin.on_unload()

    async def test_queue_dedups_same_video(self, plugin):
        """同一视频重复入队只保留一条。"""
        await self._queue_one(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg())
        assert len(plugin._bg_pending["stream-1"]) == 1
        await plugin.on_unload()

    async def test_queue_bounded_per_session(self, plugin):
        """单会话队列上限：超出丢最旧（防堆积）。"""
        await plugin.on_load()
        self._stub_client_vision(plugin)
        for i in range(plugin._BG_MAX_PENDING_PER_SESSION + 2):
            await plugin._bg_frame_vision_task(
                VideoTarget(f"BV{i:010d}"), self._msg())
        entries = plugin._bg_pending["stream-1"]
        assert len(entries) == plugin._BG_MAX_PENDING_PER_SESSION
        # 最旧的被丢弃，留下最后入队的（i = 2,3,4）
        assert entries[-1]["video_id"] == "BV0000000004"
        await plugin.on_unload()

    async def test_no_stream_id_warns_and_skips(self, plugin, caplog):
        """消息缺会话流 ID 时不入队（并留 warning）。"""
        await plugin.on_load()
        self._stub_client_vision(plugin)
        msg = {"message_info": {"group_info": {"group_id": 1},
                                "user_info": {"user_id": 456}}}
        await plugin._bg_frame_vision_task(VideoTarget("BV1xx411c7XZ"), msg)
        assert plugin._bg_pending == {}
        await plugin.on_unload()

    # ---------- 注入 ----------

    async def test_planner_items_injection(self, plugin):
        """planner 请求走 items 路径：追加一条 SystemMessageItem。"""
        await self._queue_one(plugin)
        result = await plugin.inject_planner_frame_summary(
            session_id="stream-1", item_schema_version=1,
            items=[{"item_type": "UserMessageItem", "meta": {}, "parts": []}])
        assert result["action"] == "continue"
        mod = result["modified_kwargs"]
        assert mod["item_schema_version"] == 1
        assert len(mod["items"]) == 2
        new_item = mod["items"][-1]
        assert new_item["item_type"] == "SystemMessageItem"
        assert new_item["meta"]["item_id"]
        text = new_item["parts"][0]["text"]
        assert "[B站关键帧总结" in text and "关键帧内容" in text
        await plugin.on_unload()

    async def test_replyer_items_injection(self, plugin):
        """replyer 链路同样注入（两条链路都要覆盖）。"""
        await self._queue_one(plugin)
        result = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", item_schema_version=1, items=[])
        assert "关键帧内容" in result["modified_kwargs"]["items"][-1][
            "parts"][0]["text"]
        await plugin.on_unload()

    async def test_prompt_shape_compat(self, plugin):
        """planner 载荷是 prompt 字符串时追加到末尾。"""
        await self._queue_one(plugin)
        result = await plugin.inject_planner_frame_summary(
            session_id="stream-1", prompt="原始提示词")
        mod = result["modified_kwargs"]
        assert mod["prompt"].startswith("原始提示词")
        assert "关键帧内容" in mod["prompt"]
        assert "items" not in mod
        await plugin.on_unload()

    async def test_messages_shape_compat(self, plugin):
        """messages 列表形态：追加一条 role=system。"""
        await self._queue_one(plugin)
        result = await plugin.inject_planner_frame_summary(
            session_id="stream-1", messages=[{"role": "user", "content": "hi"}])
        mod = result["modified_kwargs"]
        assert mod["messages"][-1]["role"] == "system"
        assert "关键帧内容" in mod["messages"][-1]["content"]
        await plugin.on_unload()

    async def test_idempotent_when_marker_present(self, plugin):
        """items 里已含**本视频**的 marker 时不重复注入（重试场景）。

        v1.0.16：判重由"单一全局 marker"改为"逐视频 marker"——只有**同一视频**
        的总结已在上下文里才跳过；别的视频的 marker 不得压掉当前视频
        （核心回归见 `TestV116PerVideoDedup.test_other_video_marker_does_not_suppress`）。
        队列里的视频是 `BV1xx411c7XZ`，故此处 marker 必须带**同一个** video_id
        才会命中判重；旧用例写的是 `[B站关键帧总结·旧]`（无匹配 video_id），
        在新语义下本就该注入，属于**用例前提过时**而非功能缺陷。
        """
        await self._queue_one(plugin)
        items = [{"item_type": "SystemMessageItem", "meta": {},
                  "parts": [{"type": "text",
                             "text": "[B站关键帧总结·BV1xx411c7XZ·旧的]"}]}]
        with _LogCapture(plugin) as cap:
            result = await plugin.inject_planner_frame_summary(
                session_id="stream-1", item_schema_version=1, items=items)
        assert result == {"action": "continue"}   # 未改写
        assert "已在上下文中" in cap.text, "判重命中必须留日志，不得静默（v1.0.15 教训）"
        await plugin.on_unload()

    async def test_inject_limit_then_expires(self, plugin):
        """两通道各投一次后回收（防后续多轮反复出现）。

        v1.0.14：回收判据由"全局次数上限"改为"planner + replyer 都投过"。
        """
        await self._queue_one(plugin)
        r1 = await plugin.inject_planner_frame_summary(
            session_id="stream-1", item_schema_version=1, items=[])
        assert r1.get("modified_kwargs")
        # 只投过 planner：还没进过成文模型，不能回收
        assert plugin._bg_pending.get("stream-1")
        r2 = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", item_schema_version=1, items=[])
        assert r2.get("modified_kwargs")
        # 两通道都投过 → 回收 → 后续请求无注入
        assert plugin._bg_pending.get("stream-1") in (None, [])
        r3 = await plugin.inject_planner_frame_summary(
            session_id="stream-1", item_schema_version=1, items=[])
        assert r3 == {"action": "continue"}
        await plugin.on_unload()

    async def test_ttl_expiry_blocks_injection(self, plugin):
        """超过 TTL 的待注入项不再注入。"""
        await self._queue_one(plugin)
        plugin._bg_pending["stream-1"][0]["ts"] = (
            time.time() - plugin._BG_PENDING_TTL_SEC - 1)
        r = await plugin.inject_planner_frame_summary(
            session_id="stream-1", item_schema_version=1, items=[])
        assert r == {"action": "continue"}
        assert plugin._bg_pending == {}
        await plugin.on_unload()

    async def test_other_session_not_injected(self, plugin):
        """只注入对应会话，别的会话不受影响。"""
        await self._queue_one(plugin, session_id="stream-A")
        r = await plugin.inject_planner_frame_summary(
            session_id="stream-B", item_schema_version=1, items=[])
        assert r == {"action": "continue"}
        # A 的待注入项还在
        assert plugin._bg_pending.get("stream-A")
        await plugin.on_unload()

    async def test_disabled_plugin_skips(self, plugin):
        """插件停用时 hook 直接放行。"""
        plugin.config.plugin.enabled = False
        r = await plugin.inject_planner_frame_summary(
            session_id="stream-1", items=[])
        assert r == {"action": "continue"}

    # ---------- 与前序版本的联动 ----------

    async def test_end_to_end_queue_then_inject(self, plugin):
        """端到端：hook 解析 → 后台任务 → 入队 → 下一轮 planner 注入。"""
        host = FakeHost(plugin_id=PLUGIN_ID)
        self._bind_host(plugin, host)
        await plugin.on_load()
        self._stub_client_vision(plugin)
        message = {
            "message_id": "e2e-1",
            "session_id": "stream-e2e",
            "processed_plain_text": "看这个 https://b23.tv/AbCdEf9x",
            "message_info": {"group_info": {"group_id": 1}, "user_info": {}},
        }
        await plugin.on_incoming_message(message=message)
        for t in list(plugin._bg_tasks):
            await t
        # 消息正文里只有轻量注入块，没有关键帧总结
        assert "[B站关键帧总结" not in message["processed_plain_text"]
        # 下一轮 planner 请求拿到关键帧总结
        r = await plugin.inject_planner_frame_summary(
            session_id="stream-e2e", item_schema_version=1, items=[])
        assert "关键帧内容" in r["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        # 全程没有主动发消息
        assert host.calls_of("send.text") == []
        await plugin.on_unload()


# ── v1.0.13：后台链路可观测性 / 解析后去重 / 注入计数不白烧 ─────

class _LogCapture:
    """把插件 logger 的记录收进 list。

    不用 pytest caplog：插件 logger 的 level 由外部（真机是 Host）设定，
    若为 WARNING 会让 INFO 记录在传播前就被丢弃，断言静默失败。
    """

    def __init__(self, plugin):
        self.records: list = []
        self._logger = plugin.ctx.logger
        self._handler = logging.Handler()
        self._handler.emit = self.records.append

    def __enter__(self):
        self._level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        return False

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


class FormResolvingClient(FakeBiliClient):
    """把 b23 短码解析成 BV 号的替身（模拟真实 resolve_target），
    并记录雪碧图请求次数（= VLM 前的必要步骤，用来证明去重生效）。"""

    def __init__(self, mapping: dict):
        super().__init__()
        self.mapping = mapping
        self.videoshot_hits = 0

    async def resolve_target(self, target):
        return VideoTarget(self.mapping.get(target.video_id, target.video_id),
                           page=target.page)

    async def get_video_info(self, target):
        return {"bvid": target.video_id, "cid": 1, "title": "某长视频",
                "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                "pages": 1, "page": 1}

    async def get_videoshot(self, info):
        self.videoshot_hits += 1
        return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}


class TestV113BackgroundObservability:
    """真机 16:16 复盘：后台链路"孵化→解析→信息→雪碧图→VLM→入队"全段无日志，
    日志里既看不出任务是否孵化，也看不出卡在哪一级（只能靠猜）。

    v1.0.13 要求：每一级跳过都留痕；解析后用真实 video_id 统一日志与去重键；
    marker 判重必须在"取用队列"之前，否则重试会白烧注入计数。
    """

    def _bind_host(self, plugin):
        host = FakeHost(plugin_id=PLUGIN_ID)
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        return host

    def _stub(self, plugin, vision_result="关键帧内容", mapping=None,
              duration=300, shot=True):
        client = FormResolvingClient(mapping or {})
        client._force_duration = duration
        client._force_shot = shot

        async def _info(target):
            return {"bvid": target.video_id, "cid": 1, "title": "某长视频",
                    "duration": duration, "owner_mid": 5, "desc": "",
                    "owner": "UP", "view": 1, "danmaku": 0, "like": 0,
                    "coin": 0, "pages": 1, "page": 1}

        async def _shot(info):
            client.videoshot_hits += 1
            return {"image_urls": ["http://i0.hdslb.com/x.jpg"]} if shot else None

        client.get_video_info = _info
        client.get_videoshot = _shot
        plugin._client = client

        vision_calls = {"n": 0}

        class FakeVision:
            async def analyze(self, info, shot_data, cli, urls):
                vision_calls["n"] += 1
                return vision_result

        plugin._vision = FakeVision()
        return client, vision_calls

    @staticmethod
    def _msg(session_id="stream-1"):
        return {"session_id": session_id,
                "message_info": {"group_info": {"group_id": 1},
                                 "user_info": {"user_id": 456}}}

    # ---------- 孵化阶段留痕 ----------

    async def test_spawn_logs_when_parse_flag_off(self, plugin):
        """parse.enable_frame_vision=off → 留痕说明跳过原因。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        plugin.config.parse.enable_frame_vision = False
        with _LogCapture(plugin) as cap:
            plugin._spawn_background_frame_vision(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert plugin._bg_tasks == set()
        assert "parse.enable_frame_vision=off" in cap.text
        await plugin.on_unload()

    async def test_spawn_logs_when_hook_flag_off(self, plugin):
        """trigger.enable_frame_vision_in_hook=off → 留痕说明跳过原因。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        plugin.config.trigger.enable_frame_vision_in_hook = False
        with _LogCapture(plugin) as cap:
            plugin._spawn_background_frame_vision(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert plugin._bg_tasks == set()
        assert "enable_frame_vision_in_hook=off" in cap.text
        await plugin.on_unload()

    async def test_spawn_logs_when_vision_manager_missing(self, plugin):
        """视觉管理器缺失（on_load 未跑/已 unload）→ warning，不再静默。"""
        self._bind_host(plugin)
        await plugin.on_load()
        plugin._vision = None
        with _LogCapture(plugin) as cap:
            plugin._spawn_background_frame_vision(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert plugin._bg_tasks == set()
        assert "视觉管理器未初始化" in cap.text
        await plugin.on_unload()

    async def test_spawn_logs_cooldown_and_hatch(self, plugin):
        """首次孵化留痕；冷却期内二次孵化留痕说明原因。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        target = VideoTarget("BV1xx411c7XZ")
        with _LogCapture(plugin) as cap:
            plugin._spawn_background_frame_vision(target, self._msg())
            assert "关键帧后台任务已孵化" in cap.text
            for t in list(plugin._bg_tasks):
                await t
            plugin._spawn_background_frame_vision(target, self._msg())
        assert "分钟内已识别过" in cap.text
        await plugin.on_unload()

    # ---------- 任务各级留痕 ----------

    async def test_task_logs_duration_skip(self, plugin):
        """时长不足门槛 → 留痕（不再静默 return）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin, duration=30)
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert plugin._bg_pending == {}
        assert "时长 30s < 门槛 60s" in cap.text
        await plugin.on_unload()

    async def test_task_logs_videoshot_miss(self, plugin):
        """雪碧图取不到 → 留痕。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin, shot=False)
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert plugin._bg_pending == {}
        assert "雪碧图未取到" in cap.text
        await plugin.on_unload()

    async def test_task_logs_vlm_empty(self, plugin):
        """VLM 无产出 → 必须留痕（v1.0.18 起报**真实原因**，不再写死「返回空」）。

        原前提（日志写死「VLM 返回空」）已被 v1.0.18 推翻：现在日志读
        `_vision_failure_reason()`。本桩（FakeVision）**不带** `last_failure_reason`
        ⇒ 走 `getattr` 降级为「未返回内容」。意图不变：失败**绝不静默**。
        """
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin, vision_result="")
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert plugin._bg_pending == {}
        assert "关键帧后台识别未产出结果" in cap.text, "失败必须留痕，不得静默"
        assert "未返回内容" in cap.text, "桩无 reason 属性 → 应降级为笼统说法"
        await plugin.on_unload()

    async def test_task_logs_completion_with_elapsed(self, plugin):
        """成功路径留痕：完成 + 耗时。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert "关键帧后台任务完成" in cap.text
        assert "耗时" in cap.text
        await plugin.on_unload()

    async def test_task_logs_failed_stage(self, plugin):
        """异常留痕：带失败阶段名（等价于"卡在哪一级"的答案）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        client, _ = self._stub(plugin)

        async def _boom(info):
            raise RuntimeError("雪碧图接口炸了")

        client.get_videoshot = _boom
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        assert "关键帧后台任务失败" in cap.text
        assert "阶段：取雪碧图" in cap.text
        await plugin.on_unload()

    # ---------- 解析后按真实 video_id 去重 / 统一日志键 ----------

    async def test_queue_entry_uses_resolved_video_id(self, plugin):
        """b23 短码入队时记的是解析后的真实 BV 号（与 L1/L2 日志同键可 grep）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin, mapping={"b23:AbCdEf9x": "BV1xx411c7XZ"})
        with _LogCapture(plugin) as cap:
            plugin._spawn_background_frame_vision(
                VideoTarget("b23:AbCdEf9x"), self._msg())
            for t in list(plugin._bg_tasks):
                await t
        entry = plugin._bg_pending["stream-1"][0]
        assert entry["video_id"] == "BV1xx411c7XZ"
        # 孵化发生在解析之前，只有原始短码可记 —— 如实记录
        hatched = [r.getMessage() for r in cap.records
                   if "已孵化" in r.getMessage()]
        assert hatched and "b23:AbCdEf9x" in hatched[0]
        # 解析之后的各级日志统一用真实 BV 号
        after = [r.getMessage() for r in cap.records
                 if "已孵化" not in r.getMessage()]
        assert after, "应有入队/完成日志"
        assert all("BV1xx411c7XZ" in m for m in after), after
        await plugin.on_unload()

    async def test_cross_form_dedup_avoids_second_vlm(self, plugin):
        """同一视频先以 BV 链接、后以 b23 短链分享 → 第二次不再跑 VLM。

        v1.0.12 只用原始 target 的 cache_key 去重，两种链接形态 key 不同
        （`b23:xxx#p1` vs `bvxxx#p1`）→ 漏判，白跑一次雪碧图+VLM。
        """
        self._bind_host(plugin)
        await plugin.on_load()
        client, vision_calls = self._stub(
            plugin, mapping={"b23:AbCdEf9x": "BV1xx411c7XZ"})
        # 第一次：显式 BV 链接
        plugin._spawn_background_frame_vision(
            VideoTarget("BV1xx411c7XZ"), self._msg())
        for t in list(plugin._bg_tasks):
            await t
        assert client.videoshot_hits == 1
        # 第二次：同一视频的 b23 短链
        with _LogCapture(plugin) as cap:
            plugin._spawn_background_frame_vision(
                VideoTarget("b23:AbCdEf9x"), self._msg())
            for t in list(plugin._bg_tasks):
                await t
        assert client.videoshot_hits == 1, "同一视频不应重复跑雪碧图+VLM"
        assert vision_calls["n"] == 1
        assert "另一链接形态" in cap.text
        assert len(plugin._bg_pending["stream-1"]) == 1
        await plugin.on_unload()

    # ---------- 注入侧：缺会话 ID 可见化 + 注入计数不白烧 ----------

    async def test_inject_warns_once_when_session_id_missing(self, plugin):
        """hook 载荷无 session_id/chat_id → 提示一次（否则整个功能静默失效）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        with _LogCapture(plugin) as cap:
            for _ in range(3):
                r = await plugin.inject_planner_frame_summary(items=[])
                assert r == {"action": "continue"}
        hits = [m for m in cap.records
                if "没有 session_id" in m.getMessage()]
        assert len(hits) == 1, "同一通道只应提示一次，避免每轮刷屏"
        await plugin.on_unload()

    async def test_marker_check_does_not_burn_inject_count(self, plugin):
        """载荷已含**本视频**的 marker 时不得改写载荷，且不挤占 replyer 的机会。

        v1.0.12 是"先取用后判重"，重试会把配额白烧掉，导致 replyer 拿不到内容。
        v1.0.16 起"已在上下文"会被记为**已投递**（内容确实进了模型上下文），
        但通道登记是 set 语义、天然幂等，所以真正要守的不变量是：
        **planner 反复重试不会占掉 replyer 的那一格**。
        """
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg())
        entry = plugin._bg_pending["stream-1"][0]
        assert entry["injected"] == 0
        # 模拟"本视频的总结已在本轮上下文里"（重试场景）
        items = [{"item_type": "SystemMessageItem", "meta": {},
                  "parts": [{"type": "text",
                             "text": "[B站关键帧总结·BV1xx411c7XZ·某长视频]"}]}]
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                session_id="stream-1", item_schema_version=1, items=items)
        assert r == {"action": "continue"}, "已在上下文 → 不改写载荷"
        # 必须有日志：判重不得是静默出口（v1.0.16）
        assert "已在上下文中" in cap.text
        assert entry["channels"] == {"planner"}, "只登记当前通道"
        # 关键不变量：planner 的重试没有占掉 replyer 的机会
        r2 = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", item_schema_version=1, items=[])
        assert r2.get("modified_kwargs"), "replyer 仍须拿到这一格"
        # 两通道都投过 → 回收
        assert plugin._bg_pending.get("stream-1") in (None, [])
        await plugin.on_unload()

    async def test_planner_and_replyer_both_get_one_turn(self, plugin):
        """一条总结正好够 planner + replyer 各注入一次。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg())
        p = await plugin.inject_planner_frame_summary(
            session_id="stream-1", items=[])
        r = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", items=[])
        assert p.get("modified_kwargs") and r.get("modified_kwargs")
        # 用尽 → 后续不再注入
        assert plugin._bg_pending.get("stream-1") in (None, [])
        await plugin.on_unload()


# ── v1.0.14：注入契约（完整 kwargs）+ 通道配额 ─────────────────

class TestV114InjectContract:
    """真机 16:33 复盘 + 开发文档核对：

    ⚠ 文档两处（《02》《03》）都写明 `modified_kwargs` 是**完整替换**整个
    kwargs（非增量合并）。只回传 `{"items", "item_schema_version"}` 会让
    planner 丢掉 `tool_definitions`（→ 工具消失、reply 都调不出来）、
    replyer 丢掉 `attempt`/`max_retries`/`task_name`/`selected_model_name`
    （→ 重试与模型选择失效）。这是 v1.0.12/v1.0.13 的潜伏缺陷：因为队列一直
    在 hook 触发之后才填充，从未真正注入过，所以一直没爆。

    另：回收判据改为"通道覆盖完整"，避免没有产出回复的规划轮次白吃配额。
    """

    def _bind_host(self, plugin):
        host = FakeHost(plugin_id=PLUGIN_ID)
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        return host

    def _stub(self, plugin, vision_text="关键帧内容"):
        class BgClient(FakeBiliClient):
            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "长视频",
                        "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

        plugin._client = BgClient()

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                return vision_text

        plugin._vision = FakeVision()

    @staticmethod
    def _msg(session_id="stream-1"):
        return {"session_id": session_id,
                "message_info": {"group_info": {"group_id": 1},
                                 "user_info": {"user_id": 456}}}

    async def _queue_one(self, plugin, session_id="stream-1"):
        await plugin.on_load()
        self._stub(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg(session_id))

    # ---------- 完整 kwargs 回传 ----------

    async def test_planner_modified_kwargs_keeps_all_keys(self, plugin):
        """planner 注入后除 items/item_schema_version 外的字段必须原样保留。

        真机 planner 载荷（16:34:12 诊断实录）：built_message_count,
        hook_name, item_schema_version, items, selected_history_count,
        selection_reason, session_id, tool_definitions。
        """
        self._bind_host(plugin)
        await self._queue_one(plugin)
        kwargs = {
            "hook_name": "maisaka.planner.before_request",
            "session_id": "stream-1",
            "item_schema_version": 1,
            "items": [{"item_type": "UserMessageItem", "meta": {}, "parts": []}],
            "tool_definitions": [{"name": "reply"}, {"name": "skip"}],
            "built_message_count": 27,
            "selected_history_count": 12,
            "selection_reason": "recent",
        }
        r = await plugin.inject_planner_frame_summary(**kwargs)
        mod = r["modified_kwargs"]
        # 关键字段一个都不能少
        assert mod["tool_definitions"] == [{"name": "reply"}, {"name": "skip"}], \
            "丢掉 tool_definitions 会让 planner 看不见任何工具"
        assert mod["built_message_count"] == 27
        assert mod["selected_history_count"] == 12
        assert mod["selection_reason"] == "recent"
        assert mod["session_id"] == "stream-1"
        assert mod["hook_name"] == "maisaka.planner.before_request"
        # 注入生效
        assert len(mod["items"]) == 2
        assert "关键帧内容" in mod["items"][-1]["parts"][0]["text"]
        await plugin.on_unload()

    async def test_replyer_modified_kwargs_keeps_all_keys(self, plugin):
        """replyer 注入后 retry / 模型选择等字段必须原样保留。

        真机 replyer 载荷（16:34:35 诊断实录）：attempt, hook_name,
        item_schema_version, items, max_retries, reply_message_id,
        reply_reason, reply_tool_args, request_type, requested_model_name,
        retry_count, selected_expression_ids, selected_model_name,
        selected_model_visual, session_id, task_name。
        """
        self._bind_host(plugin)
        await self._queue_one(plugin)
        kwargs = {
            "hook_name": "maisaka.replyer.before_model_request",
            "session_id": "stream-1",
            "item_schema_version": 1,
            "items": [],
            "attempt": 2, "retry_count": 1, "max_retries": 3,
            "task_name": "replyer", "request_type": "reply",
            "requested_model_name": "LongCat-2.5-Preview",
            "selected_model_name": "db-glm-5-2-260617",
            "selected_model_visual": False,
            "reply_message_id": "2096561099",
            "reply_reason": "自然回应",
            "reply_tool_args": {"msg_id": "1"},
            "selected_expression_ids": [],
        }
        r = await plugin.inject_replyer_frame_summary(**kwargs)
        mod = r["modified_kwargs"]
        for key in ("attempt", "retry_count", "max_retries", "task_name",
                    "request_type", "selected_model_name",
                    "selected_model_visual", "reply_message_id",
                    "reply_reason", "reply_tool_args"):
            assert key in mod, f"{key} 被丢掉了"
        assert mod["attempt"] == 2 and mod["max_retries"] == 3
        assert mod["selected_model_name"] == "db-glm-5-2-260617"
        assert len(mod["items"]) == 1
        await plugin.on_unload()

    async def test_prompt_shape_keeps_all_keys(self, plugin):
        """prompt 形态同样回传完整 kwargs。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        r = await plugin.inject_planner_frame_summary(
            session_id="stream-1", prompt="原始提示词",
            tool_definitions=[{"name": "reply"}], built_message_count=5)
        mod = r["modified_kwargs"]
        assert mod["prompt"].startswith("原始提示词")
        assert mod["tool_definitions"] == [{"name": "reply"}]
        assert mod["built_message_count"] == 5
        await plugin.on_unload()

    # ---------- 通道配额 ----------

    async def test_planner_only_round_keeps_entry_for_replyer(self, plugin):
        """关键契约：没有产出回复的规划轮次不得吃掉配额。

        真机 16:34 第二轮 planner 就是这种轮次（结论"无需调用任何工具"，
        根本没有 replyer）。若用全局计数，这轮就把配额吃掉一格甚至两格，
        真正成文的那次 replyer 反而拿不到内容。
        """
        self._bind_host(plugin)
        await self._queue_one(plugin)
        # 第 1 轮：只有 planner（该轮未产出回复）
        r1 = await plugin.inject_planner_frame_summary(
            session_id="stream-1", items=[])
        assert r1.get("modified_kwargs")
        # 第 2 轮：planner 已投过 → 不再重复注入（不刷屏）
        r2 = await plugin.inject_planner_frame_summary(
            session_id="stream-1", items=[])
        assert r2 == {"action": "continue"}
        # 但成文模型必须仍然拿得到
        r3 = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", items=[])
        assert r3.get("modified_kwargs"), "replyer 必须还能拿到内容"
        assert "关键帧内容" in r3["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        # 两通道都投过 → 回收
        assert plugin._bg_pending.get("stream-1") in (None, [])
        await plugin.on_unload()

    async def test_replyer_first_then_planner_also_recycles(self, plugin):
        """顺序无关：replyer 先投、planner 后投，同样回收。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        r1 = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", items=[])
        assert r1.get("modified_kwargs")
        assert plugin._bg_pending.get("stream-1"), "只投过 replyer，不回收"
        r2 = await plugin.inject_planner_frame_summary(
            session_id="stream-1", items=[])
        assert r2.get("modified_kwargs")
        assert plugin._bg_pending.get("stream-1") in (None, [])
        await plugin.on_unload()

    async def test_ttl_still_recycles_unfinished_entry(self, plugin):
        """通道没收全但已过期：TTL 兜底回收（防长期滞留）。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        r = await plugin.inject_planner_frame_summary(
            session_id="stream-1", items=[])
        assert r.get("modified_kwargs")
        plugin._bg_pending["stream-1"][0]["ts"] = (
            time.time() - plugin._BG_PENDING_TTL_SEC - 1)
        r2 = await plugin.inject_replyer_frame_summary(
            session_id="stream-1", items=[])
        assert r2 == {"action": "continue"}
        assert plugin._bg_pending == {}
        await plugin.on_unload()

    # ---------- 分级耗时日志 ----------

    async def test_completion_logs_per_stage_timing(self, plugin):
        """完成日志必须给出分级耗时（否则 63.7s 是雪碧图还是 VLM 无从判断）。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        # on_load + stub 已在 _queue_one 里跑过，这里重新跑一次任务以捕获日志
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ2"), self._msg("stream-2"))
        done = [r.getMessage() for r in cap.records
                if "关键帧后台任务完成" in r.getMessage()]
        assert done, cap.text
        msg = done[0]
        for stage in ("解析短链", "取视频信息", "取雪碧图", "VLM 识别"):
            assert stage in msg, f"缺少 {stage} 的分级耗时: {msg}"
        await plugin.on_unload()

    async def test_failure_log_carries_completed_stage_timing(self, plugin):
        """失败日志除失败阶段外，还要带上"已完成那几级"的耗时。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)

        class BoomClient(FakeBiliClient):
            async def resolve_target(self, target):
                return target

            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "长视频",
                        "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                raise RuntimeError("雪碧图接口炸了")

        plugin._client = BoomClient()
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1xx411c7XZ"), self._msg())
        fails = [r.getMessage() for r in cap.records
                 if "关键帧后台任务失败" in r.getMessage()]
        assert fails
        assert "阶段：取雪碧图" in fails[0]
        assert "解析短链" in fails[0] and "取视频信息" in fails[0]
        await plugin.on_unload()


class TestV115InjectDiagnostics:
    """真机 17:20 死局复盘（v1.0.15）。

    现象：关键帧总结 **10.5s** 就入队，距 planner hook 触发还有 13s
    （队列已就绪 —— 这是 v1.0.12 以来第一次"内容备好后才开轮"的干净窗口），
    两个 hook 都该触发，但注入日志一行都没有。当时无法定位，因为所有非命中
    路径都是裸 return。本版把三种根因全部变成日志事实：

      ① hook 根本没执行              → 每通道首次触发打探针（载荷字段/session/实例 id）；
      ② 入队与注入 session_id 不同源 → 用"120s 内**别的**会话刚入过队"点出；
      ③ 队列被清空（插件重载）        → 用"曾入队却查不到 + 实例 id"点出。

    同时必须**保持正常轮次安静**（大多数轮次本就没有待注入内容），
    否则每轮两条噪音会淹没真信号 —— 这是本类一半用例在守的边界。
    """

    SID = "79588bdc971fac73433f5ec062aa1576"   # 真机狸猫私聊会话键

    def _bind_host(self, plugin):
        host = FakeHost(plugin_id=PLUGIN_ID)
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        return host

    def _stub(self, plugin, vision_text="关键帧内容"):
        class BgClient(FakeBiliClient):
            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "长视频",
                        "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

        plugin._client = BgClient()

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                return vision_text

        plugin._vision = FakeVision()

    @staticmethod
    def _msg(session_id):
        return {"session_id": session_id,
                "message_info": {"group_info": {"group_id": 1},
                                 "user_info": {"user_id": 456}}}

    @staticmethod
    def _planner_kwargs(session_id):
        """真机 17:20:39 planner 载荷字段（cv-lyric-context 诊断实录）。

        注意真机清单里**没有 prompt/messages**，只有 items —— 所以 items
        分支是唯一有效注入路径，另两条是兼容性兜底。
        """
        return {
            "hook_name": "maisaka.planner.before_request",
            "session_id": session_id, "item_schema_version": 1,
            "items": [{"item_type": "UserMessageItem", "meta": {}, "parts": []}],
            "tool_definitions": [{"name": "reply"}, {"name": "skip"}],
            "built_message_count": 27, "selected_history_count": 12,
            "selection_reason": "recent",
        }

    @staticmethod
    def _replyer_kwargs(session_id):
        """真机 17:20:53 replyer 载荷字段（cv-lyric-context 诊断实录）。"""
        return {
            "hook_name": "maisaka.replyer.before_model_request",
            "session_id": session_id, "item_schema_version": 1, "items": [],
            "attempt": 1, "retry_count": 0, "max_retries": 3,
            "task_name": "replyer", "request_type": "reply",
            "requested_model_name": "LongCat-2.5-Preview",
            "selected_model_name": "db-glm-5-2-260617",
            "selected_model_visual": False,
            "reply_message_id": "120501402", "reply_reason": "回应分享",
            "reply_tool_args": {"msg_id": "120501402"},
            "selected_expression_ids": [],
        }

    async def _queue_one(self, plugin, session_id=None):
        await plugin.on_load()
        self._stub(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1xx411c7XZ"), self._msg(session_id or self.SID))

    @staticmethod
    def _bg_enqueued_at(plugin, session_id=None):
        """该会话最近一次入队时间（诊断痕迹，v1.0.16 起只按时间裁剪）。"""
        return plugin._bg_enqueued.get(session_id or TestV115InjectDiagnostics.SID, 0.0)

    @staticmethod
    def _bg_served_at(plugin, session_id=None):
        """该会话最近一次"队列被正常清空"的时间（v1.0.16 新增）。"""
        return plugin._bg_served.get(session_id or TestV115InjectDiagnostics.SID, 0.0)

    # ---------- ① hook 到底有没有执行：首次探针 ----------

    async def test_probe_fires_once_per_channel(self, plugin):
        """每通道首次触发 hook 打一次探针（载荷字段 + 会话键 + 实例 id）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
            first = [r.getMessage() for r in cap.records
                     if "关键帧注入探针" in r.getMessage()]
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
            again = [r.getMessage() for r in cap.records
                     if "关键帧注入探针" in r.getMessage()]
        assert len(first) == 1, "首次触发必须有探针"
        assert len(again) == 1, "探针只该打一次，否则每轮刷屏"
        assert "planner" in first[0]
        assert "tool_definitions" in first[0], "要能看出载荷字段清单"
        assert self.SID in first[0]
        assert hex(id(plugin)) in first[0], "实例 id 用于验证是否发生重载"
        await plugin.on_unload()

    async def test_probe_fires_even_without_session_id(self, plugin):
        """载荷缺 session_id 时探针仍要打 —— 否则"hook 没执行"无从排除。"""
        self._bind_host(plugin)
        await plugin.on_load()
        kwargs = self._planner_kwargs(self.SID)
        kwargs.pop("session_id")
        with _LogCapture(plugin) as cap:
            await plugin.inject_replyer_frame_summary(**kwargs)
        assert "关键帧注入探针" in cap.text
        assert "没有 session_id" in cap.text
        await plugin.on_unload()

    # ---------- 正常轮次必须安静（防刷屏） ----------

    async def test_normal_turn_stays_silent(self, plugin):
        """队列本来就空（没有视频消息的轮次）→ 一律安静。"""
        self._bind_host(plugin)
        await plugin.on_load()
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
        assert r == {"action": "continue"}
        assert "关键帧注入未命中" not in cap.text
        assert "关键帧注入异常" not in cap.text
        assert "待注入" not in cap.text
        await plugin.on_unload()

    async def test_already_served_channel_stays_silent(self, plugin):
        """同一次请求的 retry：本通道已投过 → 安静（正常链路）。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        await plugin.inject_planner_frame_summary(
            **self._planner_kwargs(self.SID))
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
        assert "关键帧注入未命中" not in cap.text
        assert "关键帧注入异常" not in cap.text
        await plugin.on_unload()

    async def test_normal_recycle_clears_trace(self, plugin):
        """两通道各投一次后正常回收 → 记录"正常结束"，后续轮次不得误报。

        v1.0.16：痕迹清理方式变了 —— `_bg_enqueued` **保留**（它是证据），
        改用 `_bg_served` 记录"正常结束"。判据是 served 时间不早于入队时间。
        """
        self._bind_host(plugin)
        await self._queue_one(plugin)
        await plugin.inject_planner_frame_summary(
            **self._planner_kwargs(self.SID))
        await plugin.inject_replyer_frame_summary(
            **self._replyer_kwargs(self.SID))
        assert self.SID not in plugin._bg_pending
        assert self._bg_served_at(plugin) >= self._bg_enqueued_at(plugin), \
            "正常回收必须留下'正常结束'记录，否则下一轮会误报'未命中'（假阳性）"
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
        assert "关键帧注入未命中" not in cap.text
        await plugin.on_unload()

    async def test_silent_after_ttl_expired(self, plugin):
        """入队痕迹已过 TTL（正常过期回收）→ 不算异常，保持安静。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        plugin._bg_pending.clear()
        plugin._bg_enqueued[self.SID] = time.time() - (
            plugin._BG_PENDING_TTL_SEC + 100)
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
        assert "关键帧注入未命中" not in cap.text
        await plugin.on_unload()

    # ---------- ② / ③ 两种"查不到"根因必须点出来 ----------

    async def test_warns_when_queue_cleared_after_enqueue(self, plugin):
        """根因③：曾入队 → 队列被清空（模拟 on_unload 的 clear）→ 必须告警。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        assert self.SID in plugin._bg_pending
        plugin._bg_pending.clear()        # 只清队列，保留实例与痕迹表
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
        assert r == {"action": "continue"}
        assert "关键帧注入未命中" in cap.text
        assert "已查不到该项" in cap.text
        assert self.SID in cap.text, "会话键要打完整值，便于与入队日志逐字对账"
        assert hex(id(plugin)) in cap.text
        await plugin.on_unload()

    async def test_warns_when_session_ids_differ(self, plugin):
        """根因②：入队与注入两侧 session_id 不同源 —— 最隐蔽，必须点出来。"""
        self._bind_host(plugin)
        await self._queue_one(plugin, session_id="stream-enqueue")
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs("stream-inject"))
        assert "从未入过队" in cap.text
        assert "不同源" in cap.text
        assert "stream-enqueue" in cap.text, "要列出真实入队键，便于对账"
        await plugin.on_unload()

    async def test_warns_on_abnormal_entry(self, plugin):
        """队列有本会话的项、本通道没投过，却取不出文本（entry 结构异常）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        plugin._bg_pending[self.SID] = [{"text": "", "channels": set()}]
        with _LogCapture(plugin) as cap:
            plugin._diag_no_inject(self.SID, "planner", "测试")
        assert "队列有 1 条却取不出文本" in cap.text
        await plugin.on_unload()

    async def test_missing_payload_uses_unified_diag(self, plugin):
        """载荷没有 prompt/messages/items 时也走统一诊断（不再裸 return）。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        plugin._bg_pending.clear()
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                hook_name="maisaka.planner.before_request", session_id=self.SID)
        assert r == {"action": "continue"}
        assert "关键帧注入未命中" in cap.text
        assert "无 prompt/messages/items" in cap.text
        await plugin.on_unload()

    # ---------- 节流与状态清理 ----------

    async def test_warning_throttled_within_60s(self, plugin):
        """同一 (通道, 会话) 60s 内只提示一次，避免真机上刷屏。"""
        self._bind_host(plugin)
        await self._queue_one(plugin)
        plugin._bg_pending.clear()
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
            n1 = sum("关键帧注入未命中" in r.getMessage()
                     for r in cap.records)
            await plugin.inject_planner_frame_summary(
                **self._planner_kwargs(self.SID))
            n2 = sum("关键帧注入未命中" in r.getMessage()
                     for r in cap.records)
        assert n1 == 1
        assert n2 == 1, "60s 内重复轮次不得重复告警"
        await plugin.on_unload()

    async def test_enqueue_log_carries_full_key_and_instance(self, plugin):
        """入队日志打**完整**会话键 + 实例 id（否则无法与 hook 侧逐字对账）。"""
        self._bind_host(plugin)
        with _LogCapture(plugin) as cap:
            await self._queue_one(plugin)
        line = [r.getMessage() for r in cap.records
                if "关键帧总结已入待注入队列" in r.getMessage()][0]
        assert self.SID in line, "会话键必须是完整值，不能只打前 8 字符"
        assert hex(id(plugin)) in line
        await plugin.on_unload()

    async def test_unload_clears_diagnostic_state(self, plugin):
        """on_unload 必须清掉诊断状态，避免实例复用时误判。"""
        self._bind_host(plugin)
        await plugin.on_load()
        plugin._probed_channels.add("planner")
        plugin._bg_enqueued["s"] = 1.0
        plugin._bg_served["s"] = 2.0
        plugin._diag_last["planner:s"] = 1.0
        await plugin.on_unload()
        assert plugin._probed_channels == set()
        assert plugin._bg_enqueued == {}
        assert plugin._bg_served == {}
        assert plugin._diag_last == {}


class TestV116PerVideoDedup:
    """真机 17:35 复盘（v1.0.16）：三联对账**全部通过**，却依然不注入。

    记录事实：`关键帧注入探针：planner hook 首次触发…session_id='79588bdc…'，
    实例 0x260d5c9fbc0`（17:35:33）与 `关键帧注入探针：replyer hook 首次触发…`
    （17:36:21）都出现，且与 `关键帧总结已入待注入队列（会话 79588bdc…；
    实例 0x260d5c9fbc0）`（17:35:36）的**会话键与实例 id 逐字一致**：

    | 设计内根因 | 证据 | 结论 |
    |---|---|---|
    | ① hook 没执行 | 探针出现 | 排除 |
    | ② 两侧会话键不同源 | 键逐字一致 | 排除 |
    | ③ 插件重载清空队列 | 实例 id 一致 | 排除 |

    三种全排除、又没有未命中告警 ⇒ 逐条排查后只剩一个**完全静默**的出口：
    判重。而当时的判重用的是**单一全局 marker** —— 一旦某次注入成功、该 marker
    随上下文留存，之后**每个**视频都会被判成"已注入过"而永久静默跳过。

    本版把判重改为**逐视频**（`[B站关键帧总结·{video_id}`），并让命中判重
    **有日志、按已投递处理**；同时补掉诊断自身的静默盲区（`_bg_enqueued` 不再
    随队列删除联动清理）。要求：`_inject_pending` 内**再无静默 return**。
    """

    SID = "79588bdc971fac73433f5ec062aa1576"

    def _bind_host(self, plugin):
        host = FakeHost(plugin_id=PLUGIN_ID)
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        return host

    def _stub(self, plugin, vision_text="关键帧内容"):
        class BgClient(FakeBiliClient):
            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "长视频",
                        "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

        plugin._client = BgClient()

        class FakeVision:
            async def analyze(self, info, shot, client, urls):
                return vision_text

        plugin._vision = FakeVision()

    @staticmethod
    def _msg(session_id):
        return {"session_id": session_id,
                "message_info": {"group_info": {"group_id": 1},
                                 "user_info": {"user_id": 456}}}

    @staticmethod
    def _kwargs(session_id):
        """真机 17:35:33 / 17:36:21 实测字段（含真实 session_id 形态）。"""
        return {
            "hook_name": "maisaka.planner.before_request",
            "session_id": session_id, "item_schema_version": 1,
            "items": [{"item_type": "UserMessageItem", "meta": {}, "parts": []}],
            "tool_definitions": [{"name": "reply"}, {"name": "skip"}],
            "built_message_count": 29, "selected_history_count": 12,
            "selection_reason": "recent",
        }

    @staticmethod
    def _ctx_items_with(marker_text):
        return [{"item_type": "SystemMessageItem", "meta": {},
                 "parts": [{"type": "text", "text": marker_text}]}]

    async def _queue(self, plugin, bvid="BV1JThU6DE9X"):
        await plugin.on_load()
        self._stub(plugin)
        await plugin._bg_frame_vision_task(
            VideoTarget(bvid), self._msg(self.SID))

    # ---------- 核心回归：旧视频的 marker 不得压掉新视频 ----------

    async def test_other_video_marker_does_not_suppress(self, plugin):
        """**这是 v1.0.16 要修的那个 bug**：上下文里已有"别的视频"的总结时，
        当前视频仍必须正常注入（旧版用全局 marker 会永久跳过）。"""
        self._bind_host(plugin)
        await self._queue(plugin, bvid="BV1JThU6DE9X")
        items = self._ctx_items_with("[B站关键帧总结·BV1Bjg364EX1·旧的夏天]")
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                **{**self._kwargs(self.SID), "items": items})
        assert r.get("modified_kwargs"), "别的视频的 marker 不该压掉本视频"
        assert len(r["modified_kwargs"]["items"]) == 2
        injected = r["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "BV1JThU6DE9X" in injected, "注入文本要带稳定键，便于逐视频判重"
        assert "关键帧内容" in injected
        assert "已在上下文中" not in cap.text
        await plugin.on_unload()

    async def test_same_video_marker_skips_with_log(self, plugin):
        """本视频已在上下文 → 跳过重复注入，但**必须留日志**且记为已投递。"""
        self._bind_host(plugin)
        await self._queue(plugin, bvid="BV1JThU6DE9X")
        items = self._ctx_items_with("[B站关键帧总结·BV1JThU6DE9X·旧的]")
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                **{**self._kwargs(self.SID), "items": items})
        assert r == {"action": "continue"}
        assert "已在上下文中" in cap.text, "判重不得是静默出口（v1.0.15 的教训）"
        assert "BV1JThU6DE9X" in cap.text
        entry = plugin._bg_pending[self.SID][0]
        assert entry["channels"] == {"planner"}, "内容确已在上下文 → 按已投递处理"
        await plugin.on_unload()

    async def test_persisted_marker_repeated_rounds_stay_quiet(self, plugin):
        """marker 长期留在上下文时，重复轮次不得刷屏（只提示一次就收敛）。"""
        self._bind_host(plugin)
        await self._queue(plugin, bvid="BV1JThU6DE9X")
        items = self._ctx_items_with("[B站关键帧总结·BV1JThU6DE9X·旧的]")
        with _LogCapture(plugin) as cap:
            for _ in range(3):
                await plugin.inject_planner_frame_summary(
                    **{**self._kwargs(self.SID), "items": items})
        n = sum("已在上下文中" in r.getMessage() for r in cap.records)
        assert n == 1, f"同一 (通道, 视频) 只该提示一次，实际 {n} 次"
        await plugin.on_unload()

    async def test_enqueued_item_still_injectable_after_skipped_probe(self, plugin):
        """端到端复刻 17:35 时间线：planner 空队列先行 → replyer 必须注入。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        # 17:35:33 planner hook：**入队之前**触发，队列还是空的 → 静默（正常）
        r1 = await plugin.inject_planner_frame_summary(
            **{**self._kwargs(self.SID), "items": []})
        assert r1 == {"action": "continue"}
        # 17:35:36 后台任务入队（真机距 planner hook 3s）
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1JThU6DE9X"), self._msg(self.SID))
        assert plugin._bg_pending.get(self.SID)
        # 17:36:21 replyer hook：队列已就绪 45s → 必须注入
        with _LogCapture(plugin) as cap:
            r2 = await plugin.inject_replyer_frame_summary(
                **{**self._kwargs(self.SID), "items": []})
        assert r2.get("modified_kwargs"), "队列已就绪却没注入 = v1.0.15 的死局"
        assert "关键帧总结已注入replyer上下文" in cap.text
        assert "关键帧注入未命中" not in cap.text
        await plugin.on_unload()

    # ---------- 诊断盲区必须被补掉 ----------

    async def test_queue_lost_after_enqueue_always_warns(self, plugin):
        """曾入队 + 队列没了 + 无"正常结束"记录 → **必然告警**（v1.0.15 会静默）。"""
        self._bind_host(plugin)
        await self._queue(plugin)
        assert self.SID in plugin._bg_enqueued
        plugin._bg_pending.pop(self.SID, None)     # 模拟队列被清空
        plugin._bg_pending.clear()
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(
                **self._kwargs(self.SID))
        assert "关键帧注入未命中" in cap.text
        assert "也没有正常结束记录" in cap.text
        assert self.SID in cap.text and hex(id(plugin)) in cap.text
        await plugin.on_unload()

    async def test_served_record_keeps_quiet(self, plugin):
        """有"正常结束"记录 → 安静（不得把正常回收报成异常）。"""
        self._bind_host(plugin)
        await self._queue(plugin)
        await plugin.inject_planner_frame_summary(**self._kwargs(self.SID))
        await plugin.inject_replyer_frame_summary(**self._kwargs(self.SID))
        assert self.SID not in plugin._bg_pending
        assert self.SID in plugin._bg_served
        with _LogCapture(plugin) as cap:
            await plugin.inject_planner_frame_summary(**self._kwargs(self.SID))
        assert "关键帧注入未命中" not in cap.text
        await plugin.on_unload()

    async def test_multi_video_queue_injects_both(self, plugin):
        """队列里多条（不同视频）时一并注入，且各自带自己的稳定键。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        for bvid in ("BV1JThU6DE9X", "BV1Bjg364EX1"):
            await plugin._bg_frame_vision_task(
                VideoTarget(bvid), self._msg(self.SID))
        r = await plugin.inject_planner_frame_summary(**self._kwargs(self.SID))
        injected = r["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "BV1JThU6DE9X" in injected and "BV1Bjg364EX1" in injected
        await plugin.on_unload()

    async def test_partial_presence_injects_only_missing(self, plugin):
        """队列两条、上下文里已有其中一条 → 只注入缺的那条（不重复、不漏）。"""
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        for bvid in ("BV1JThU6DE9X", "BV1Bjg364EX1"):
            await plugin._bg_frame_vision_task(
                VideoTarget(bvid), self._msg(self.SID))
        items = self._ctx_items_with("[B站关键帧总结·BV1Bjg364EX1·旧的]")
        r = await plugin.inject_planner_frame_summary(
            **{**self._kwargs(self.SID), "items": items})
        injected = r["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "BV1JThU6DE9X" in injected, "缺的那条要注入"
        assert "BV1Bjg364EX1" not in injected, "已在上下文的那条不重复注入"
        await plugin.on_unload()

    # ---------- 真机 19:33 实测的投递顺序：replyer 先命中、planner 下一轮补 ----------

    async def test_replyer_first_then_planner_next_round_injects(self, plugin):
        """**v1.0.16 首次真机成功的顺序**：planner 那轮队列还空（静默）→ replyer 命中。

        真机 19:33–19:36 实测：planner hook 在轮次开始时触发（队列 19:34:06 才就绪，
        晚约 10s）故那轮空手而归；replyer 贴着请求（19:36:29）命中并注入。
        此时 `channels={'replyer'}` **不满足** `{planner, replyer}` 全覆盖 ⇒ 该项
        **必须留在队列**（TTL 15 分钟），好让下一轮 planner 补上 —— 本用例锁住这个
        顺序无关性（`test_inject_limit_then_expires` 覆盖的是 planner→replyer 的
        相反顺序）。
        """
        self._bind_host(plugin)
        await plugin.on_load()
        self._stub(plugin)
        # 第 1 轮 planner：入队之前触发，队列空 → 静默（真机 19:33:56 情形）
        r0 = await plugin.inject_planner_frame_summary(
            **{**self._kwargs(self.SID), "items": []})
        assert r0 == {"action": "continue"}
        # 后台任务入队（真机 19:34:06）
        await plugin._bg_frame_vision_task(
            VideoTarget("BV1L2eS69EnT"), self._msg(self.SID))
        # replyer 命中并注入（真机 19:36:29）
        with _LogCapture(plugin) as cap:
            r1 = await plugin.inject_replyer_frame_summary(
                **{**self._kwargs(self.SID), "items": []})
        assert r1.get("modified_kwargs")
        assert "关键帧总结已注入replyer上下文" in cap.text
        assert "回传完整 kwargs" in cap.text, "必须回传完整 kwargs（v1.0.14 契约）"
        entry = plugin._bg_pending[self.SID][0]
        assert entry["channels"] == {"replyer"}
        assert entry["video_id"] == "BV1L2eS69EnT"
        # 只投过 replyer ⇒ 不得回收，下一轮 planner 仍要拿到
        assert plugin._bg_pending.get(self.SID), "只投 replyer 就回收 = 规划层永远看不到画面"
        # 下一轮 planner（载荷不带 marker）→ 补投成功，随后两通道齐 → 回收
        r2 = await plugin.inject_planner_frame_summary(
            **{**self._kwargs(self.SID), "items": []})
        assert r2.get("modified_kwargs"), "下一轮 planner 应能补投"
        assert plugin._bg_pending.get(self.SID) in (None, [])
        await plugin.on_unload()

    async def test_persisted_injection_marks_planner_delivered(self, plugin):
        """宿主若**持久化**了 hook 注入项：下一轮 planner 判重命中并登记已投递。

        真机"待验证②"的两种可能之一（见 README）。两种都必须有日志 + 正确回收，
        本用例锁住"持久化"这一支：命中→记 planner→两通道齐→回收→此后安静。
        """
        self._bind_host(plugin)
        await self._queue(plugin, bvid="BV1L2eS69EnT")
        await plugin.inject_replyer_frame_summary(
            **{**self._kwargs(self.SID), "items": []})
        assert plugin._bg_pending[self.SID][0]["channels"] == {"replyer"}
        # 下一轮 planner 的 items 里**带着**注入项（宿主持久化）
        items = self._ctx_items_with("[B站关键帧总结·BV1L2eS69EnT·猫娘计划]")
        with _LogCapture(plugin) as cap:
            r = await plugin.inject_planner_frame_summary(
                **{**self._kwargs(self.SID), "items": items})
        assert r == {"action": "continue"}, "已在上下文 → 不得重复注入"
        assert "已在上下文中" in cap.text
        assert plugin._bg_pending.get(self.SID) in (None, []), "两通道齐 → 应回收"
        await plugin.on_unload()

    async def test_marker_branch_is_observable(self, plugin):
        """纪律性用例：守住 v1.0.16 的修复不被回退 —— 判重分支必须留日志。

        真机教训：判重一旦是静默 `return {}`，"三联对账全部通过却查不到原因"
        就会重演。这条用源码检查守住（行为用例在
        `test_same_video_marker_skips_with_log` 里另有一份）。
        """
        import inspect
        src = inspect.getsource(type(plugin)._inject_pending)
        assert "已在上下文中" in src, "判重命中必须有日志，不得静默"
        assert "_diag_no_inject" in src, "未注入原因必须经统一诊断出口"

    # ---------- v1.0.17：注入日志必须报「实际投递的视频」 ----------

    async def test_injection_log_names_delivered_video_and_queue_state(self, plugin):
        """注入日志的尾段必须是**本次投递的视频**，并说明队列去向。

        真机 19:36:45 现象：`关键帧总结已注入planner上下文（items，回传完整 kwargs
        8 键）: —` —— 注入**成功**了，尾部却是 `—`。原因：该行原本读的是
        "注入**之后**队列还剩什么"，而这次投递正好让两通道投齐、条目被当场回收
        ⇒ 读到空 ⇒ `—`，读起来像"什么都没注入"（一个纯诊断性的自伤）。
        """
        self._bind_host(plugin)
        await self._queue(plugin, bvid="BV1L2eS69EnT")
        # replyer 先投（真机 19:36:29）：此时条目仍在队列
        with _LogCapture(plugin) as cap1:
            r1 = await plugin.inject_replyer_frame_summary(**self._kwargs(self.SID))
        assert r1.get("modified_kwargs")
        line1 = [r.getMessage() for r in cap1.records
                 if "已注入replyer上下文" in r.getMessage()][0]
        assert "BV1L2eS69EnT" in line1, f"日志必须报出实际投递的视频: {line1}"
        assert "仍在队列" in line1, f"只投一通道应说明仍留在队列: {line1}"
        # planner 补投（真机 19:36:45）：两通道投齐 → 当场回收，日志仍须报出视频
        with _LogCapture(plugin) as cap2:
            r2 = await plugin.inject_planner_frame_summary(**self._kwargs(self.SID))
        assert r2.get("modified_kwargs")
        line2 = [r.getMessage() for r in cap2.records
                 if "已注入planner上下文" in r.getMessage()][0]
        assert "BV1L2eS69EnT" in line2, (
            f"投齐后被回收也不许打成 `: —`（真机 19:36:45 的原缺陷）: {line2}")
        assert "已回收" in line2, f"应说明队列已清空: {line2}"
        await plugin.on_unload()


# ══════════════════════════════════════════════════════════════
# v1.0.18：关键帧失败原因必须报真话（超时 ≠ 返回空）
# ══════════════════════════════════════════════════════════════

class TestV118VisionFailureReason:
    """真机 19:55:13 复盘（v1.0.18）：RPC 超时被误报成「VLM 返回空」。

    实测日志两行挨着出现：

        [WARNING] 关键帧识别失败: [E_TIMEOUT] 请求 cap.call 超时 (85000ms)
        [INFO]    关键帧后台识别未产出结果（VLM 返回空，耗时 85.5s）: BV1ezKf6GEqx

    第二行是**谎报**：第一行已说清是"cap.call 超时 85000ms"，第二行却写成
    "VLM 返回空"。根因是**双层超时**的顺序——`frame_vision._call_llm` 是
    `wait_for(90s)` 包 `call_capability(..., timeout_ms=85000)`：

    | 触发的超时 | 抛出的异常 | 是否 `asyncio.TimeoutError` |
    |---|---|---|
    | 外层 `wait_for(90s)` | `asyncio.TimeoutError` | ✅ |
    | **内层 RPC 85000ms**（真机走的就是这条） | SDK 的 `RPCError`（文本 `[E_TIMEOUT] …`） | ❌ |

    内层**先于**外层触发 ⇒ 走的是通用 `except Exception` 分支，被吞成
    "识别失败"；调用方拿到 `None` 又一概说成"VLM 返回空"。后果：把排查方向
    从"调大超时 / 换更快模型"带偏到"改提示词 / 换模型"。

    修复两处：① `frame_vision._is_timeout()` 精确认超时（含 `RPCError` 形态），
    `FrameVisionManager.last_failure_reason` 记真原因；② `plugin` 日志改读真原因，
    并做 `getattr` 兼容降级（测试桩无该属性也不许抛）。
    """

    SID = "79588bdc971fac73433f5ec062aa1576"

    def _bind(self, plugin, host=None):
        host = host or FakeHost(plugin_id=PLUGIN_ID)
        ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
        bind_context(plugin, ctx, get_default_config(
            getattr(type(plugin), "config_model", None)))
        return host

    @staticmethod
    def _msg(session_id):
        return {"session_id": session_id,
                "message_info": {"group_info": {"group_id": 1},
                                 "user_info": {"user_id": 456}}}

    # ---------- ① _is_timeout 必须认得出「内层 RPC 超时」 ----------

    def test_is_timeout_recognizes_inner_rpc_timeout(self):
        """`RPCError` 形态（文本含 `E_TIMEOUT` / "超时"）必须判为超时。

        真机原文：`[E_TIMEOUT] 请求 cap.call 超时 (85000ms)`。它**不是**
        `asyncio.TimeoutError`，只靠 isinstance 会漏判。
        """
        from frame_vision import _is_timeout
        inner = RuntimeError("[E_TIMEOUT] 请求 cap.call 超时 (85000ms)")
        assert _is_timeout(inner) is True, "内层 RPC 超时被误判为非超时"

    def test_is_timeout_covers_standard_and_named_timeouts(self):
        from frame_vision import _is_timeout
        assert _is_timeout(asyncio.TimeoutError()) is True
        assert _is_timeout(TimeoutError()) is True

        class SomeTimeoutError(Exception):
            pass

        assert _is_timeout(SomeTimeoutError("boom")) is True, "类名含 timeout 也要认"
        assert _is_timeout(RuntimeError("operation timeout")) is True

    def test_is_timeout_excludes_genuine_errors(self):
        """真·异常不得被吞成超时（否则又丢真原因）。"""
        from frame_vision import _is_timeout
        assert _is_timeout(ValueError("bad json")) is False
        assert _is_timeout(RuntimeError("能力未声明: llm.generate")) is False
        assert _is_timeout(KeyError("missing")) is False

    # ---------- ② 管理器把真原因记进 last_failure_reason ----------

    async def test_describe_frames_rpc_timeout_records_reason(self, plugin):
        """内层 RPC 超时 → reason 含"超时"，且日志不再说"返回空"。"""

        class TimeoutHost(FakeHost):
            async def rpc_call(self, method, plugin_id="", payload=None, **kw):
                cap = (payload or {}).get("capability") or method
                if cap == "llm.generate":
                    # 复刻 SDK 内层 RPC 超时的真实异常形态
                    raise RuntimeError("[E_TIMEOUT] 请求 cap.call 超时 (85000ms)")
                return await super().rpc_call(method, plugin_id, payload, **kw)

        self._bind(plugin, TimeoutHost(plugin_id=PLUGIN_ID))
        await plugin.on_load()          # 建真正的 FrameVisionManager
        mgr = plugin._vision
        with _LogCapture(plugin) as cap:
            out = await mgr._describe_frames([b"\xff\xd8\xff\xe0fake"], 120)
        assert out is None
        assert "超时" in mgr.last_failure_reason, (
            f"超时原因必须落进 last_failure_reason: {mgr.last_failure_reason!r}")
        assert "85000" in mgr.last_failure_reason, (
            f"reason 应点明是哪一层的超时: {mgr.last_failure_reason!r}")
        assert "返回空" not in mgr.last_failure_reason, "超时不得说成返回空"
        assert "VLM 返回空" not in cap.text
        assert "关键帧识别超时（RPC 85000ms）" in cap.text, "超时要有专门的告警文案"
        await plugin.on_unload()

    async def test_empty_vlm_text_still_reported_as_empty(self, plugin):
        """反向锁定：真·空文本仍要报"空"，别被这次修复带偏成"超时"。"""
        host = FakeHost(plugin_id=PLUGIN_ID,
                        returns={"llm.generate": {"success": True, "response": "   "}})
        self._bind(plugin, host)
        await plugin.on_load()
        mgr = plugin._vision
        out = await mgr._describe_frames([b"fake"], 120)
        assert out is None
        assert "空文本" in mgr.last_failure_reason, (
            f"真·空文本要照实报空: {mgr.last_failure_reason!r}")
        assert "超时" not in mgr.last_failure_reason
        await plugin.on_unload()

    # ---------- ③ 插件后台日志读真原因（真机 19:55:13 的那一行） ----------

    async def test_background_log_uses_real_reason_not_empty(self, plugin):
        """后台任务失败时，日志必须报真原因，不得再写死"VLM 返回空"。"""
        self._bind(plugin)
        await plugin.on_load()

        class BgClient(FakeBiliClient):
            async def get_video_info(self, target):
                return {"bvid": target.video_id, "cid": 1, "title": "长视频",
                        "duration": 300, "owner_mid": 5, "desc": "", "owner": "UP",
                        "view": 1, "danmaku": 0, "like": 0, "coin": 0,
                        "pages": 1, "page": 1}

            async def get_videoshot(self, info):
                return {"image_urls": ["http://i0.hdslb.com/x.jpg"]}

        plugin._client = BgClient()

        class FakeVision:
            last_failure_reason = "帧描述超时（RPC 85000ms 内未返回）"

            async def analyze(self, info, shot, client, urls):
                return None

        plugin._vision = FakeVision()
        with _LogCapture(plugin) as cap:
            await plugin._bg_frame_vision_task(
                VideoTarget("BV1ezKf6GEqx"), self._msg(self.SID))
        assert "关键帧后台识别未产出结果（帧描述超时（RPC 85000ms 内未返回）" in cap.text, (
            f"必须报真实原因（真机 19:55:13 的缺陷）: {cap.text}")
        assert "VLM 返回空" not in cap.text, "超时绝不能再被写成「返回空」"
        assert plugin._bg_pending.get(self.SID) in (None, []), "失败不该入队"
        await plugin.on_unload()

    async def test_failure_reason_getattr_fallback(self, plugin):
        """测试桩/旧实现没有 `last_failure_reason` → 降级为笼统说法，绝不抛。"""

        class FakeVision:                       # 故意不带该属性
            async def analyze(self, info, shot, client, urls):
                return None

        plugin._vision = FakeVision()
        assert plugin._vision_failure_reason() == "未返回内容"

        plugin._vision = None
        assert plugin._vision_failure_reason() == "未返回内容", "无视觉组件也要降级不抛"

    # ---------- ④ 纪律性用例：守住日志不再写死"返回空" ----------

    def test_source_logs_reason_variable_not_hardcoded(self, plugin):
        """源码级守卫：后台失败的日志必须引用 `reason`，不得写死"返回空"。

        真机教训：写死文案会把"超时"这类真原因盖掉，排查方向被带偏。
        注意这里只查**日志文案**，不查注释/文档串（v1.0.18 的说明性注释里会
        引用旧字样"VLM 返回空"作对比，那种出现是正当的）。
        """
        import inspect
        src = inspect.getsource(type(plugin)._bg_frame_vision_task)
        assert "关键帧后台识别未产出结果（{reason}" in src, \
            "失败原因必须经 `reason` 变量插值"
        assert "未产出结果（VLM 返回空" not in src, \
            "不得再把「VLM 返回空」写死进日志文案"

    def test_frame_vision_records_reason_on_every_failure_branch(self):
        """源码级守卫：`analyze`/`_describe_frames`/`_summarize` 的失败出口
        都要落 `last_failure_reason`，避免又出现"有失败但没原因"的盲区。
        """
        import inspect
        from frame_vision import FrameVisionManager
        for meth in ("analyze", "_describe_frames", "_summarize"):
            src = inspect.getsource(getattr(FrameVisionManager, meth))
            assert "last_failure_reason" in src, f"{meth} 的失败出口未记原因"
