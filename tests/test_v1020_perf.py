"""v1.0.20 性能/内存专项回归用例。

覆盖计划的 9 项改动中可行为化验证的部分：
  P1 `_inject_pending` 空队列短路（不拼 payload，但探针/session 告警保留）
  P2 `_has_target_hint` 预筛接线（普通消息不跑完整正则，卡片兜底不受影响）
  P3 后台关键帧任务复用 hook 透传的 real_target/info（零重复 API）
  P4 `_injected_recently` 有界化
  P5 `_diag_last` 有界化
  P7 配置更新显式关闭旧 httpx client（不再泄漏连接池）

v1.0.21（真机 09-30 20:41 复盘）：
  关键帧总结 20:41:57 入队，低活跃群首次 planner 请求 20:59:04（1027s），
  超旧 TTL 900s 被静默清掉 —— TTL 放宽 7200 + 过期未投满必须留痕。
"""
import logging
import sys
import time
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


class _CountingClient:
    """记录 resolve_target / get_video_info 调用次数的假 client。"""

    def __init__(self, info: dict):
        self.info = info
        self.resolve_calls = 0
        self.info_calls = 0

    async def resolve_target(self, target):
        self.resolve_calls += 1
        return target

    async def get_video_info(self, _target):
        self.info_calls += 1
        return self.info

    async def get_ai_conclusion(self, _info):
        return None

    async def get_videoshot(self, _info):
        return None

    async def get_subtitle(self, _info):
        return None

    async def close(self):
        pass


_INFO = {"bvid": "BV1xx411c7XZ", "cid": 1, "title": "某视频", "desc": "",
         "duration": 125, "owner": "某UP", "owner_mid": 1, "view": 1,
         "danmaku": 0, "like": 0, "coin": 0, "favorite": 0,
         "pages": 1, "page": 1}


class TestP1EmptyQueueShortCircuit:
    """P1：空队列时不得构建 payload 文本（每次模型请求省一次全上下文拼接）。

    短路位置必须在 session_id 校验**之后**——缺 session_id 的告警是
    "注入功能整体失效"的唯一可见信号，不能被吞（v1.0.20 首轮实现就栽在这，
    被 test_probe_fires_even_without_session_id 抓回）。
    """

    async def test_empty_queue_does_not_build_payload(self, plugin, monkeypatch):
        await plugin.on_load()
        calls = []
        # 注意：fixture 经 load_plugin_module 包式加载，插件类不属于
        # `import plugin` 那个模块实例 —— 必须 patch 实例所属的类。
        orig = type(plugin)._payload_text

        @classmethod
        def _spy(cls, prompt, messages, items):
            calls.append(1)
            return orig(prompt, messages, items)

        monkeypatch.setattr(type(plugin), "_payload_text", _spy)
        result = plugin._inject_pending(
            {"session_id": "s1", "items": [{"parts": [{"type": "text", "text": "x" * 5000}]}]},
            "planner")
        assert result == {}
        assert not calls, "空队列时 _payload_text 不应被调用"
        await plugin.on_unload()

    async def test_non_empty_queue_still_builds_payload(self, plugin, monkeypatch):
        """反向验证：队列有内容时短路不得误伤正常注入链。"""
        await plugin.on_load()
        calls = []
        orig = type(plugin)._payload_text

        @classmethod
        def _spy(cls, prompt, messages, items):
            calls.append(1)
            return orig(prompt, messages, items)

        monkeypatch.setattr(type(plugin), "_payload_text", _spy)
        plugin._bg_pending["s1"] = [{
            "video_id": "BV1xx411c7XZ", "title": "t", "text": "总结正文",
            "ts": time.time(), "injected": 0, "channels": set(),
        }]
        result = plugin._inject_pending(
            {"session_id": "s1", "items": []}, "planner")
        assert calls, "队列有内容时 _payload_text 必须参与判重"
        assert result.get("items"), "正常注入链应产出 modified items"
        await plugin.on_unload()


class TestP2TargetHintGate:
    """P2：普通消息不跑完整提取正则；hint 命中才跑。"""

    def _message(self, text: str) -> dict:
        return {"processed_plain_text": text, "message_id": "m1",
                "session_id": "s1"}

    def _spy_extract(self, plugin, monkeypatch, calls):
        # 插件经 load_plugin_module 包式加载为独立模块实例，
        # patch 必须打在实例所属模块的命名空间上
        mod = sys.modules[type(plugin).__module__]
        orig = mod.extract_targets_from_text

        def _spy(text):
            calls.append(1)
            return orig(text)

        monkeypatch.setattr(mod, "extract_targets_from_text", _spy)

    async def test_plain_message_skips_extract(self, plugin, monkeypatch):
        await plugin.on_load()
        calls = []
        self._spy_extract(plugin, monkeypatch, calls)
        result = await plugin.on_incoming_message(
            message=self._message("今天天气怎么样"))
        assert result == {"action": "continue"}
        assert not calls, "无 B 站痕迹的消息不应触发 extract_targets_from_text"
        await plugin.on_unload()

    async def test_hint_hit_still_extracts(self, plugin, monkeypatch):
        await plugin.on_load()
        plugin._client = _CountingClient(_INFO)
        calls = []
        self._spy_extract(plugin, monkeypatch, calls)
        result = await plugin.on_incoming_message(
            message=self._message("看看 BV1xx411c7XZ"))
        assert calls, "hint 命中后必须走完整提取"
        assert result.get("modified_kwargs") is not None, "命中后应完成注入"
        await plugin.on_unload()


class TestP3BackgroundReuse:
    """P3：后台任务拿到 hook 透传的 real_target/info 时零重复 API。"""

    async def test_bg_task_reuses_info(self, plugin):
        await plugin.on_load()
        client = _CountingClient(_INFO)
        plugin._client = client
        message = {"session_id": "s1"}
        target = VideoTarget("BV1xx411c7XZ")
        await plugin._bg_frame_vision_task(
            target, message, real_target=target, info=dict(_INFO))
        assert client.resolve_calls == 0, "透传 real_target 后不得重复 resolve"
        assert client.info_calls == 0, "透传 info 后不得重复请求 view 接口"
        await plugin.on_unload()

    async def test_bg_task_falls_back_without_info(self, plugin):
        """缓存命中等 info 缺失场景，保持原有的重新请求路径。"""
        await plugin.on_load()
        client = _CountingClient(_INFO)
        plugin._client = client
        message = {"session_id": "s1"}
        target = VideoTarget("BV1xx411c7XZ")
        await plugin._bg_frame_vision_task(target, message)
        assert client.resolve_calls == 1
        assert client.info_calls == 1
        await plugin.on_unload()


class TestP4P5BoundedDicts:
    """P4/P5：两处此前只写不删的字典必须有界。"""

    def test_injected_recently_is_bounded(self, plugin):
        now = time.time()
        plugin._injected_recently = {
            f"k{i}": now - 1000 for i in range(600)}  # 全部超 300s 窗
        plugin._note_injected_recently("new-key")
        assert len(plugin._injected_recently) <= 513
        assert "new-key" in plugin._injected_recently
        # 超窗旧键应被清掉
        assert "k0" not in plugin._injected_recently

    def test_diag_last_is_bounded(self, plugin):
        now = time.time()
        plugin._diag_last = {
            f"planner:s{i}": now - 1000 for i in range(300)}
        plugin._diag_mark("planner:new", now)
        assert len(plugin._diag_last) <= 201
        assert plugin._diag_last["planner:new"] == now


class _LogCapture:
    """把插件 logger 的记录收进 list（不用 caplog，理由同 test_parser.py）。"""

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


class TestV1021PendingTTL:
    """v1.0.21：TTL 放宽 + 过期留痕（真机 1027s 超窗静默丢失复盘）。"""

    @staticmethod
    def _entry(ts: float, channels=()) -> dict:
        return {"video_id": "BV1isao6NEAd", "title": "高塔之上",
                "text": "总结正文", "ts": ts, "injected": len(channels),
                "channels": set(channels)}

    async def test_entry_survives_old_900s_window(self, plugin):
        """真机场景：入队 1027s 后首次模型请求，旧 900s TTL 会清掉，新 7200s 必须存活。"""
        await plugin.on_load()
        now = time.time()
        plugin._bg_pending["s1"] = [self._entry(now - 1027)]
        plugin._purge_pending(now=now)
        assert plugin._bg_pending.get("s1"), (
            "1027s 的条目在新 TTL 下必须存活（真机场景：低活跃群 17 分钟后才思考）")
        await plugin.on_unload()

    async def test_expired_undelivered_is_logged(self, plugin):
        """过期且未投满的条目：清掉 + INFO 留痕（不再静默丢失）。"""
        await plugin.on_load()
        now = time.time()
        plugin._bg_pending["s1"] = [
            self._entry(now - 7300, channels={"planner"})]  # 只投过 1/2 通道
        with _LogCapture(plugin) as cap:
            plugin._purge_pending(now=now)
        assert not plugin._bg_pending.get("s1"), "超 7200s 的条目仍应被清理"
        assert "过期未投完" in cap.text, "过期未投满必须留痕"
        assert "BV1isao6NEAd" in cap.text
        await plugin.on_unload()

    async def test_fully_delivered_purged_silently(self, plugin):
        """两通道投完的正常回收：清掉但不打「过期未投完」日志。"""
        await plugin.on_load()
        now = time.time()
        plugin._bg_pending["s1"] = [
            self._entry(now - 100, channels={"planner", "replyer"})]
        with _LogCapture(plugin) as cap:
            plugin._purge_pending(now=now)
        assert not plugin._bg_pending.get("s1")
        assert "过期未投完" not in cap.text, "正常回收不得告警"
        await plugin.on_unload()

    async def test_expired_but_delivered_is_silent(self, plugin):
        """过期但已投满两通道：正常回收，不告警。"""
        await plugin.on_load()
        now = time.time()
        plugin._bg_pending["s1"] = [
            self._entry(now - 7300, channels={"planner", "replyer"})]
        with _LogCapture(plugin) as cap:
            plugin._purge_pending(now=now)
        assert not plugin._bg_pending.get("s1")
        assert "过期未投完" not in cap.text
        await plugin.on_unload()


class TestP7ClientSwapOnConfigUpdate:
    """P7：配置更新必须显式关闭旧 httpx client（不再泄漏连接池）。"""

    async def test_update_methods_report_change(self):
        from bili_video import BiliVideoClient
        c = BiliVideoClient(sessdata="A", timeout_sec=15.0)
        assert c.update_sessdata("A") is False, "同值不应触发重建"
        assert c.update_sessdata("B") is True
        assert c.update_timeout(15.0) is False
        assert c.update_timeout(30.0) is True
        await c.close()

    async def test_old_client_closed_on_swap(self):
        from bili_video import BiliVideoClient
        c = BiliVideoClient(sessdata="A")
        old = await c._ensure_client()
        assert not old.is_closed
        if c.update_sessdata("B"):
            await c._swap_client()
        assert old.is_closed, "旧 client 必须被显式关闭"
        new = await c._ensure_client()
        assert new is not old and not new.is_closed
        await c.close()

    async def test_on_config_update_closes_old_client(self, plugin):
        await plugin.on_load()
        old = await plugin._client._ensure_client()
        plugin.config.credential.sessdata = "CHANGED_SESSDATA"
        await plugin.on_config_update("credential", {}, "1")
        assert old.is_closed, "on_config_update 必须关闭携带旧凭据的 client"
        await plugin.on_unload()

    async def test_no_change_no_swap(self, plugin):
        """配置无变化时不得重建（避免无谓的连接池抖动）。"""
        await plugin.on_load()
        old = await plugin._client._ensure_client()
        await plugin.on_config_update("plugin", {"plugin": {"config_version": "1"}}, "1")
        assert not old.is_closed
        await plugin.on_unload()
