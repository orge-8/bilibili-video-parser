"""pytest 行为测试：链接提取、降级链、格式化、manifest 反向断言。

    python -m pytest tests/test_parser.py -v
"""

import asyncio
import json
import sys
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

    async def test_hook_skips_frame_vision_by_default(self, plugin):
        """hook 路径默认不跑关键帧（enable_frame_vision_in_hook=False）。"""
        await plugin.on_load()
        assert plugin.config.trigger.enable_frame_vision_in_hook is False
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
