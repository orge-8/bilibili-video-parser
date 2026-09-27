"""v1.0.19 安全修复回归 —— maibot-plugin-audit 第 1/2/7/13 项。

全部用例都是**出站行为断言**：用 `httpx.MockTransport` 记录每一次请求的
`(host, cookie)`，证明「凭据不会发给非 B 站域」「白名单外的域连请求都收不到」。
这类结论只有跑起来才算证据 —— 静态阅读只能给出「可疑」。

修复前的实测证据（本文件即为回归）：
  - 媒体域 302 → evil 域，evil **收到** `SESSDATA=<明文>`（凭据外发，高）
  - b23.tv 302 → 非白名单域，该域**已收到**请求（逐跳白名单失效，中）
"""
import asyncio
import sys
import time
import types
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bili_video  # noqa: E402
import frame_vision  # noqa: E402
import plugin as plugin_module  # noqa: E402
from bili_video import BiliVideoClient, VideoTarget, redact_secrets  # noqa: E402

SESSDATA = "SECRET_SESSDATA_VALUE"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 128


def _install_transport(monkeypatch, mapping, hits):
    """把 `_new_client` 换成「与真实实现同参数 + MockTransport」的版本。

    真实 `_new_client` 的 `follow_redirects=False` / 空 cookie 由
    `test_new_client_contract` 单独守卫，两者合起来才覆盖完整行为。
    """
    async def handler(request):
        hits.append((request.url.host, request.headers.get("cookie", "")))
        fn = mapping.get(request.url.host)
        if fn is None:
            return httpx.Response(200, content=PNG, request=request)
        return fn(request)

    def patched(timeout_sec):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers=dict(bili_video._HEADERS),
            follow_redirects=False,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(BiliVideoClient, "_new_client",
                        staticmethod(patched))


def _redirect(location):
    def fn(request):
        return httpx.Response(302, headers={"location": location},
                              request=request)
    return fn


# ── 客户端契约（守卫 _new_client 本身） ──────────────────────

class TestClientContract:
    def test_new_client_contract(self):
        """_new_client 不得预置 cookie，且必须关闭自动重定向。"""
        async def run():
            c = BiliVideoClient._new_client(5.0)
            try:
                return c.follow_redirects, len(c.cookies)
            finally:
                await c.aclose()

        follow, cookie_count = asyncio.run(run())
        assert follow is False, "自动重定向会绕过白名单/带走凭据"
        assert cookie_count == 0, "cookie 必须按域显式写入，不能在构造期预置"


# ── audit 第 1 项：凭据外发 ──────────────────────────────────

class TestCredentialEgress:
    def test_api_host_receives_scoped_credentials(self, monkeypatch):
        """正向：凭据仍要能到 api.bilibili.com（修复不能把功能一起杀掉）。"""
        hits = []

        def api(request):
            return httpx.Response(200, json={"code": 0, "data": {}},
                                  request=request)

        _install_transport(monkeypatch, {"api.bilibili.com": api}, hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                await c._get_json("https://api.bilibili.com/x/web-interface/view",
                                  {"bvid": "BV1Sq4y167Rj"})
            finally:
                await c.close()

        asyncio.run(run())
        api_cookies = [ck for h, ck in hits if h == "api.bilibili.com"]
        assert api_cookies, "未发出请求"
        assert SESSDATA in api_cookies[0]
        assert "buvid3=" in api_cookies[0]

    def test_media_redirect_to_foreign_host_never_leaks(self, monkeypatch):
        """反向：媒体域 302 到外部域 → 该域不得收到任何凭据（修复前会外发）。"""
        hits = []
        _install_transport(monkeypatch,
                           {"i0.hdslb.com": _redirect("https://evil.example.com/steal")},
                           hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                return await c.download_image("https://i0.hdslb.com/bfs/sprite.jpg")
            finally:
                await c.close()

        data = asyncio.run(run())
        assert data is None
        hosts = [h for h, _ in hits]
        assert "evil.example.com" not in hosts, f"非白名单域已收到请求: {hits}"
        assert hosts == ["i0.hdslb.com"]
        assert all(SESSDATA not in ck for _, ck in hits), f"凭据外泄: {hits}"

    def test_subtitle_body_fetch_is_cookieless(self, monkeypatch):
        """字幕 JSON 走无 cookie 通道，且重定向到外部域即放弃。

        注意 nav 必须返回**真实的 32 位 wbi key**：`_build_mixin_key` 会把
        两个文件名拼起来按索引表取 32 字符，太短的假 key 会让签名流程提前失败，
        请求根本到不了字幕域（第一次写这条用例就踩了）。
        """
        hits = []
        key_a, key_b = "a" * 32, "b" * 32

        def nav(request):
            return httpx.Response(200, json={"code": 0, "data": {"wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{key_a}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{key_b}.png",
            }}}, request=request)

        def wbi(request):
            return httpx.Response(200, json={"code": 0, "data": {"subtitle": {
                "subtitles": [{"lan": "zh-CN",
                               "subtitle_url": "https://aisubtitle.hdslb.com/s.json"}]}}},
                request=request)

        _install_transport(monkeypatch, {
            "api.bilibili.com": lambda r: nav(r) if "/nav" in str(r.url) else wbi(r),
            "aisubtitle.hdslb.com": _redirect("https://evil.example.com/steal"),
        }, hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                return await c.get_subtitle({"bvid": "BV1Sq4y167Rj", "cid": 1})
            finally:
                await c.close()

        text = asyncio.run(run())
        assert text is None
        assert "evil.example.com" not in [h for h, _ in hits]
        sub_cookies = [ck for h, ck in hits if h == "aisubtitle.hdslb.com"]
        assert sub_cookies == [""], f"字幕域不应带 cookie: {sub_cookies}"

    def test_media_redirect_within_whitelist_still_works(self, monkeypatch):
        """无回归：媒体域内部跳转（hdslb → hdslb）仍能正常下载。"""
        hits = []
        _install_transport(monkeypatch, {
            "i0.hdslb.com": _redirect("https://i1.hdslb.com/bfs/sprite.jpg"),
            "i1.hdslb.com": lambda r: httpx.Response(200, content=PNG, request=r),
        }, hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                return await c.download_image("https://i0.hdslb.com/bfs/sprite.jpg")
            finally:
                await c.close()

        assert asyncio.run(run()) == PNG
        assert [h for h, _ in hits] == ["i0.hdslb.com", "i1.hdslb.com"]


# ── audit 第 2 项：SSRF / 逐跳白名单 ─────────────────────────

class TestShortlinkWhitelist:
    def test_foreign_redirect_never_contacted(self, monkeypatch):
        """短链跳到非白名单域（含内网 IP）→ 该域连请求都不该收到。"""
        hits = []
        _install_transport(monkeypatch,
                           {"b23.tv": _redirect("https://169.254.169.254/latest/meta-data/")},
                           hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                try:
                    await c.resolve_target(VideoTarget("b23:AbCdEf9x"))
                    return None
                except ValueError as e:
                    return str(e)
            finally:
                await c.close()

        msg = asyncio.run(run())
        assert msg is not None and "非白名单域" in msg
        assert [h for h, _ in hits] == ["b23.tv"], f"越界请求: {hits}"
        assert all(ck == "" for _, ck in hits), "短链通道不得携带凭据"

    def test_whitelisted_hop_resolves(self, monkeypatch):
        """无回归：白名单内的跳转（b23.tv → www.bilibili.com）正常解析。"""
        hits = []
        _install_transport(monkeypatch, {
            "b23.tv": _redirect("https://www.bilibili.com/video/BV1Sq4y167Rj"),
            "www.bilibili.com": lambda r: httpx.Response(200, content=b"ok",
                                                         request=r),
        }, hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                return await c.resolve_target(VideoTarget("b23:AbCdEf9x"))
            finally:
                await c.close()

        target = asyncio.run(run())
        assert target.video_id == "BV1Sq4y167Rj"
        assert [h for h, _ in hits] == ["b23.tv", "www.bilibili.com"]

    def test_get_json_rejects_redirect_without_echoing_target(self, monkeypatch):
        """接口 3xx 判失败，且异常文本不回显跳转目标（防半盲探测）。"""
        hits = []
        _install_transport(monkeypatch,
                           {"api.bilibili.com": _redirect("https://evil.example.com/x")},
                           hits)

        async def run():
            c = BiliVideoClient(sessdata=SESSDATA)
            try:
                try:
                    await c._get_json("https://api.bilibili.com/x/web-interface/view")
                    return None
                except ValueError as e:
                    return str(e)
            finally:
                await c.close()

        msg = asyncio.run(run())
        assert msg is not None and "重定向" in msg
        assert "evil.example.com" not in msg


# ── audit 第 7 项：日志脱敏 ──────────────────────────────────

class TestLogRedaction:
    def test_redact_secrets_masks_values_keeps_keys(self):
        """值全部吃掉、键名保留（以便排障）。"""
        raw = "adapter error: p_skey=deadbeef uin=123456789 token=abcd1234"
        out = redact_secrets(raw)
        for secret in ("deadbeef", "123456789", "abcd1234"):
            assert secret not in out, f"凭据泄漏: {secret} in {out}"
        assert out.count("<redacted>") == 3
        for key in ("p_skey", "uin", "token"):
            assert key in out, f"键名应保留以便排障: {out}"

    def test_redact_secrets_handles_cookie_prefix(self):
        """`cookies=SESSDATA=x` 形态（整段被当作值吃掉也算安全）。"""
        out = redact_secrets("boom: cookies=SESSDATA=SECRETVAL; p_skey=deadbeef")
        assert "SECRETVAL" not in out and "deadbeef" not in out, out
        assert "<redacted>" in out

    def test_redact_secrets_leaves_normal_text_alone(self):
        """反向用例：正常文本不得被误伤（防脱敏把排障信息吃掉）。"""
        raw = "视频解析命中 L1 官方总结: BV1Sq4y167Rj，耗时 3.2s"
        assert redact_secrets(raw) == raw

    def test_napcat_adapter_error_is_redacted(self):
        """适配器异常消息内嵌 cookie 时，日志不得出现明文（audit 第 7 项坑）。"""
        logs = []

        class _Logger:
            def warning(self, msg, *a, **k):
                logs.append(str(msg))

            info = warning

            def error(self, msg, *a, **k):
                logs.append(str(msg))

        async def boom(*a, **k):
            raise RuntimeError(
                "adapter error: cookies=SESSDATA=SECRETVAL; p_skey=deadbeef")

        p = plugin_module.BilibiliVideoParserPlugin()
        # ctx 是只读 property（SDK 由 Runner 经 _set_context 注入），不能直接赋值
        p._set_context(types.SimpleNamespace(logger=_Logger(), call_capability=boom))
        asyncio.run(p._napcat_get_msg("12345"))

        joined = "\n".join(logs)
        assert joined, "未产生日志（该分支必须留痕）"
        assert "SECRETVAL" not in joined and "deadbeef" not in joined, joined
        assert "<redacted>" in joined


# ── audit 第 13 项：同步 CPU 工作不得阻塞事件循环 ────────────

class TestFrameExtractionOffload:
    """切帧/编码是同步 CPU 操作，必须丢线程池；否则 bot 期间收不到消息。

    门槛校准（本机实测 3 次，Windows + Python 3.12，busy 0.6s / tick 0.005s）：
      - 走 `asyncio.to_thread` → ticker **18 / 18 / 18** 次
      - 同步直调（旧实现）    → ticker **0 / 0 / 0** 次
    阈值取 5：既容忍 GIL 争用（tick 速率被压到 ~30/s）造成的抖动，
    又与「阻塞」的 0 拉开数量级差距。区分性事实是 **0 与非 0**。
    """

    BUSY_SEC = 0.6
    TICK_SEC = 0.005
    MIN_TICKS = 5

    @staticmethod
    def _manager(monkeypatch):
        def slow(*a, **k):
            end = time.monotonic() + TestFrameExtractionOffload.BUSY_SEC
            while time.monotonic() < end:
                pass
            return [b"frame"]

        monkeypatch.setattr(frame_vision, "_extract_frames", slow)

        async def noop_describe(self, frames, desc_max):
            return None

        monkeypatch.setattr(frame_vision.FrameVisionManager,
                            "_describe_frames", noop_describe)

        class _Parse:
            max_frames = 4
            desc_max_chars = 100

        class _Config:
            parse = _Parse()

        class _Plugin:
            config = _Config()

        class _Client:
            async def download_image(self, url):
                return b"x" * 64

        return frame_vision.FrameVisionManager(_Plugin()), slow, _Client

    @staticmethod
    def _measure(coro_factory):
        async def run():
            ticks = 0
            step = TestFrameExtractionOffload.TICK_SEC

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(step)
                    ticks += 1

            task = asyncio.create_task(ticker())
            try:
                await coro_factory()
            finally:
                task.cancel()
            return ticks

        return asyncio.run(run())

    def test_analyze_offloads_extraction(self, monkeypatch):
        manager, _, client_cls = self._manager(monkeypatch)

        async def go():
            await manager.analyze({"title": "t", "owner": "o"}, {},
                                  client_cls(), ["https://i0.hdslb.com/x.jpg"])

        ticks = self._measure(go)
        assert ticks >= self.MIN_TICKS, (
            f"事件循环被阻塞，ticker 仅 {ticks} 次（卸载后应 ~18，阻塞时 0）")

    def test_extraction_runs_off_main_thread(self, monkeypatch):
        """直接断言执行线程：切帧必须在 worker 线程里跑。"""
        import threading
        manager, slow, client_cls = self._manager(monkeypatch)
        seen = {}

        def spy(*a, **k):
            seen["is_main"] = threading.current_thread() is threading.main_thread()
            return slow(*a, **k)

        monkeypatch.setattr(frame_vision, "_extract_frames", spy)

        async def go():
            await manager.analyze({"title": "t", "owner": "o"}, {},
                                  client_cls(), ["https://i0.hdslb.com/x.jpg"])

        self._measure(go)
        assert seen.get("is_main") is False, (
            "切帧仍在主线程执行 —— 会把事件循环堵住")

    def test_measurement_has_teeth_blocking_control(self, monkeypatch):
        """门槛校准：同步直调同一函数时 ticker 必须为 0，否则断言无牙齿。"""
        _, slow, _ = self._manager(monkeypatch)

        async def go():
            slow()  # 故意同步阻塞（旧实现的行为）

        ticks = self._measure(go)
        assert ticks == 0, f"同步路径也跑到了 {ticks} 次 —— 该断言测不出阻塞"
