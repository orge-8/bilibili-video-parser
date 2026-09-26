"""pytest 行为测试：链接提取、降级链、格式化、manifest 反向断言。

    python -m pytest tests/test_parser.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402

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
        assert "[B站视频解析]" in message["processed_plain_text"]
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
        assert "[B站视频解析]" in before
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
        await plugin.on_unload()
