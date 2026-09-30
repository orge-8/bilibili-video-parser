"""B 站视频 Web API 客户端（org.mai-mai.bilibili-video-parser）。

不依赖 bilibili-api-python，直接 httpx 调 Web API：
  - x/web-interface/view           基础信息（L3，最稳）
  - x/web-interface/view/conclusion 官方 AI 总结（L1）
  - x/player/wbi/v2                CC 字幕线索（L2a，需 WBI 签名）
  - x/player/videoshot             雪碧图关键帧（L2b）
  - b23.tv 短链解析（跟随重定向）

WBI 签名移植自 bilibili-dynamic-push/bili_client.py。
所有请求带超时；cookie（SESSDATA）可选。
"""
import asyncio
import hashlib
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

API_BASE = "https://api.bilibili.com"

# ── 链接/ID 提取 ──────────────────────────────────────────────

_BV_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")
_AV_RE = re.compile(r"\bav(\d{4,})\b", re.IGNORECASE)
_B23_RE = re.compile(r"\bhttps?://(?:www\.)?b23\.tv/([0-9A-Za-z]+)", re.IGNORECASE)
_BILI_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)(?:\?[^\s]*)?",
    re.IGNORECASE,
)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
}

# 媒体资源域白名单（字幕 JSON / 雪碧图只允许 B 站系 CDN）
_MEDIA_HOST_SUFFIXES = (".hdslb.com", ".bilibili.com", ".bimg.aqzscn.cn")
# b23 短链解析允许的重定向目标域（安全官 F-001：防止重定向到任意/内网地址）
_SHORTLINK_HOSTS = {"b23.tv", "www.bilibili.com", "m.bilibili.com", "bilibili.com"}
_SHORTLINK_MAX_HOPS = 5
# 媒体下载手动跟随重定向的最大跳数（超限即放弃，防重定向环）
_MEDIA_MAX_HOPS = 3
# 单张图片下载上限（10MB，防解压炸弹/资源耗尽）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# HTTP 重定向状态码（v1.0.19 起一律不自动跟随：见 _new_client 注释）
_REDIRECT_CODES = (301, 302, 303, 307, 308)
# 凭据 cookie 的**域限定**：只允许发给 bilibili.com 系主机（含 api.bilibili.com）
_CREDENTIAL_COOKIE_DOMAIN = ".bilibili.com"

# 日志/异常文本脱敏（audit 第 7 项）：只吃值、保留键名，兼顾排障与防泄漏
_SECRET_KV_RE = re.compile(
    r"((?:p_skey|skey|sessdata|buvid3|uin|sid|qzonetoken|pt[a-z_]*|token"
    r"|password|cookie)\s*[=:]\s*)([^\s;,&\"'）)]+)",
    re.IGNORECASE,
)


def redact_secrets(text: Any) -> str:
    """把外部来源字符串（异常对象/响应片段）里的凭据值替换为 <redacted>。

    异常消息常内嵌它请求时用的 cookie 串（`SESSDATA=xxx; ...`），
    直接 `str(e)` 落日志等于把登录态明文写进日志文件。
    """
    return _SECRET_KV_RE.sub(r"\1<redacted>", str(text or ""))

# WBI mixin key 索引表（社区共识，见 bilibili-API-collect）
_WBI_MIXIN_INDEX = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


class VideoTarget:
    """解析目标：视频 ID + 可选分 P。"""

    __slots__ = ("video_id", "page")

    def __init__(self, video_id: str, page: int = 1):
        self.video_id = video_id  # "BV..." 或 "av..."
        self.page = max(1, page)

    @property
    def cache_key(self) -> str:
        return f"{self.video_id.lower()}#p{self.page}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"VideoTarget({self.video_id!r}, p={self.page})"


def extract_targets_from_text(text: str) -> list[tuple[str, str]]:
    """从文本提取 B 站视频目标。

    返回 [(片段, 类型)]，类型：bv / av / b23 / url。
    同一 BV 只取首次出现；命令文本（/ 开头）由调用方自行过滤。
    """
    if not text:
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for m in _BILI_URL_RE.finditer(text):
        vid = _BV_RE.search(m.group(0))
        if vid:
            bv = vid.group(1)
            if bv not in seen:
                seen.add(bv)
                # 提取 ?p= 分 P
                page = 1
                try:
                    qs = parse_qs(urlparse(m.group(0)).query)
                    if qs.get("p"):
                        page = max(1, int(qs["p"][0]))
                except Exception:
                    pass
                found.append((f"{bv}?p={page}", "url"))
    for m in _BV_RE.finditer(text):
        bv = m.group(1)
        if bv not in seen:
            seen.add(bv)
            found.append((bv, "bv"))
    for m in _AV_RE.finditer(text):
        av = f"av{m.group(1)}"
        if av not in seen:
            seen.add(av)
            found.append((av, "av"))
    for m in _B23_RE.finditer(text):
        code = m.group(1)
        if code in seen:
            continue  # 1.3.0 卡片链接在 processed+raw 各出现一次，去重（v1.0.9）
        seen.add(code)
        found.append((code, "b23"))
    return found


def parse_explicit_target(target: str) -> VideoTarget | None:
    """/bili 命令参数 → VideoTarget。支持 BV/av/完整链接/短链码。"""
    s = (target or "").strip()
    if not s:
        return None
    m = _BV_RE.search(s)
    if m:
        page = 1
        if "bilibili.com" in s:
            try:
                qs = parse_qs(urlparse(s).query)
                if qs.get("p"):
                    page = max(1, int(qs["p"][0]))
            except Exception:
                pass
        return VideoTarget(m.group(1), page)
    m = _AV_RE.search(s)
    if m:
        return VideoTarget(f"av{m.group(1)}")
    m = _B23_RE.search(s)
    if m:
        return VideoTarget(f"b23:{m.group(1)}")
    return None


# ── WBI 签名 ──────────────────────────────────────────────────

def _build_mixin_key(img_url: str, sub_url: str) -> str:
    """由 nav 接口的 img_url/sub_url 文件名生成 mixin key。"""
    img_key = re.sub(r".*/", "", img_url).split(".")[0]
    sub_key = re.sub(r".*/", "", sub_url).split(".")[0]
    raw = img_key + sub_key
    return "".join(raw[i] for i in _WBI_MIXIN_INDEX)[:32]


def _wbi_sign(params: dict[str, Any], mixin_key: str) -> dict[str, Any]:
    """标准 WBI 签名：排序 urlencode + wts + md5 w_rid。"""
    signed = dict(sorted({k: str(v) for k, v in params.items() if v is not None}.items()))
    signed["wts"] = int(time.time())
    query = urlencode(signed, safe="!()*' -")
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


# ── 客户端 ────────────────────────────────────────────────────

def _is_media_host(url: str) -> bool:
    """URL host 是否在 B 站媒体域白名单内（字幕/雪碧图下载防护）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in _MEDIA_HOST_SUFFIXES)


def _page_from_url(url: str) -> int:
    """从 bilibili 视频 URL 提取 ?p= 分 P（无或非法返回 1）。"""
    try:
        qs = parse_qs(urlparse(url).query)
        if qs.get("p"):
            return max(1, int(qs["p"][0]))
    except Exception:
        pass
    return 1


class BiliVideoClient:
    """B 站视频信息客户端。所有方法失败抛异常或返回 None，不重试风暴。"""

    def __init__(self, sessdata: str = "", timeout_sec: float = 15.0):
        self._sessdata = (sessdata or "").strip()
        self._timeout = timeout_sec
        self._client: httpx.AsyncClient | None = None
        self._shortlink_client: httpx.AsyncClient | None = None  # 无 cookie，专用于短链解析
        self._media_client: httpx.AsyncClient | None = None  # 无 cookie，专用于媒体下载
        self._mixin_key = ""
        self._mixin_key_ts = 0.0

    # -- 生命周期 --

    @staticmethod
    def _new_client(timeout_sec: float) -> httpx.AsyncClient:
        """创建出站 client：**不自动跟随重定向**，且不预置任何 cookie。

        v1.0.19 安全修复（audit 第 1/2 项，均已实测复现）：
        原实现 `httpx.AsyncClient(cookies={...}, follow_redirects=True)` 有两个缺陷 ——
        ① cookie 落在**无域限定**的 jar 里，任何 302 目标都会收到 `SESSDATA`
           （实测：媒体域 302 到 evil 域后，evil 拿到完整凭据）；
        ② 白名单只作用于初始 URL，`follow_redirects=True` 会自行跟随中间跳，
           使 `_resolve_b23` 的「逐跳校验」形同虚设（实测：非白名单域已收到请求）。
        现在：重定向由调用方**手动**处理并逐跳校验白名单；凭据按域写入见 `_ensure_client`。
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers=dict(_HEADERS),
            follow_redirects=False,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            client = self._new_client(self._timeout)
            # 凭据写入**域限定** cookie jar：只会发给 .bilibili.com 系主机，
            # 跨主机重定向时 httpx 按 jar 重新派生 cookie，非该域拿不到任何凭据。
            client.cookies.set("buvid3", self._get_buvid3(),
                               domain=_CREDENTIAL_COOKIE_DOMAIN, path="/")
            if self._sessdata:
                client.cookies.set("SESSDATA", self._sessdata,
                                   domain=_CREDENTIAL_COOKIE_DOMAIN, path="/")
            self._client = client
        return self._client

    def _get_buvid3(self) -> str:
        """随机 buvid3（search 类接口无此 cookie 常被 -412 风控）。

        格式仿真实值: UUIDINFOC。每次进程生成一次，存实例复用。
        """
        if not getattr(self, "_buvid3", ""):
            import uuid
            self._buvid3 = f"{uuid.uuid4()}INFOC"
        return self._buvid3

    async def _ensure_shortlink_client(self) -> httpx.AsyncClient:
        """短链解析专用：不带 SESSDATA（防凭据随重定向外泄）。"""
        if self._shortlink_client is None or self._shortlink_client.is_closed:
            self._shortlink_client = self._new_client(self._timeout)
        return self._shortlink_client

    async def _ensure_media_client(self) -> httpx.AsyncClient:
        """媒体下载专用：不带任何 cookie。

        凭据与「能不能取图」解耦（audit 第 1 项坑 B）：域白名单只决定
        **要不要带凭据**，不决定能不能取 —— 新 CDN 不在名单里也应能下载。
        """
        if self._media_client is None or self._media_client.is_closed:
            self._media_client = self._new_client(self._timeout)
        return self._media_client

    async def close(self) -> None:
        for attr in ("_client", "_shortlink_client", "_media_client"):
            c: httpx.AsyncClient | None = getattr(self, attr)
            if c is not None and not c.is_closed:
                try:
                    await c.aclose()
                except Exception:
                    pass
            setattr(self, attr, None)

    async def _swap_client(self) -> None:
        """关闭并重建业务 client（cookie/超时变化时调用，防旧 client 泄漏）。"""
        old = self._client
        self._client = None
        if old is not None and not old.is_closed:
            try:
                await old.aclose()
            except Exception:
                pass

    def update_sessdata(self, sessdata: str) -> bool:
        """更新 SESSDATA。返回 True 表示有变化，调用方应触发 client 重建
        （异步上下文里 `await _swap_client()`，显式关闭旧 client 的连接池）。

        v1.0.20 之前这里直接 `self._client = None`：旧 AsyncClient 未 aclose()，
        每次 Runner 推配置泄漏一个连接池（socket 靠 GC 兜底）。
        """
        sd = (sessdata or "").strip()
        if sd == self._sessdata:
            return False
        self._sessdata = sd
        return True

    def update_timeout(self, timeout_sec: float) -> bool:
        """更新超时。返回 True 表示有变化，调用方应触发 client 重建。"""
        t = max(1.0, float(timeout_sec or 15.0))
        if t == self._timeout:
            return False
        self._timeout = t
        return True

    # -- 内部 --

    async def _get_json(self, url: str, params: dict[str, Any] | None = None,
                        extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        client = await self._ensure_client()
        resp = await client.get(url, params=params, headers=extra_headers)
        # 3xx 一律判失败：接口不需要跳转，跳转即异常（不回显跳转目标，防半盲探测）
        if resp.status_code in _REDIRECT_CODES:
            raise ValueError(f"接口返回重定向({resp.status_code})，已拒绝跟随")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"B站接口返回非对象: {type(data)}")
        return data

    async def _resolve_b23(self, code: str) -> str:
        """b23.tv 短链 → 目标 URL。逐跳手动跟随并校验域名白名单（防 SSRF）。

        使用无 cookie 专用 client；**每一跳**都校验白名单，任一跳落在名单外即抛
        ValueError。v1.0.19：改为手动跟随（`follow_redirects=False`）——
        此前依赖 client 自动跟随，白名单实际只作用于初始 URL，中间跳会先发出去。
        """
        client = await self._ensure_shortlink_client()
        url = f"https://b23.tv/{code}"
        for _hop in range(_SHORTLINK_MAX_HOPS):
            host = (urlparse(url).hostname or "").lower()
            if host not in _SHORTLINK_HOSTS:
                raise ValueError(f"短链重定向到非白名单域: {host}")
            resp = await client.get(url)
            try:
                if resp.status_code in _REDIRECT_CODES:
                    loc = resp.headers.get("location") or ""
                    if not loc:
                        raise ValueError("短链返回重定向但缺少 Location")
                    url = urljoin(url, loc)
                    continue
                return str(resp.url)
            finally:
                await resp.aclose()
        raise ValueError("短链重定向次数过多")

    async def _media_get(self, url: str) -> httpx.Response | None:
        """媒体域 GET：手动跟随重定向，**每一跳都校验媒体域白名单**。

        不用 client 自动重定向：自动跟随会让白名单只作用于初始 URL。
        返回的响应由调用方负责 `aclose()`；不在白名单/跳数超限返回 None。
        """
        client = await self._ensure_media_client()
        for _hop in range(_MEDIA_MAX_HOPS):
            if not _is_media_host(url):
                return None
            resp = await client.get(url)
            if resp.status_code in _REDIRECT_CODES:
                loc = resp.headers.get("location") or ""
                await resp.aclose()
                if not loc:
                    return None
                url = urljoin(url, loc)
                continue
            return resp
        return None

    async def _ensure_mixin_key(self) -> str:
        """nav 接口取 wbi_img 并缓存 mixin key（半小时刷新一次）。"""
        if self._mixin_key and time.time() - self._mixin_key_ts < 1800:
            return self._mixin_key
        data = await self._get_json(f"{API_BASE}/x/web-interface/nav")
        wbi_img = (data.get("data") or {}).get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        if not img_url or not sub_url:
            raise ValueError(f"nav 未返回 wbi_img: code={data.get('code')}")
        self._mixin_key = _build_mixin_key(img_url, sub_url)
        self._mixin_key_ts = time.time()
        return self._mixin_key

    async def resolve_target(self, target: VideoTarget) -> VideoTarget:
        """b23: 前缀目标 → 解析出真实 BV/av。其余原样返回。"""
        vid = target.video_id
        if not vid.startswith("b23:"):
            return target
        code = vid[4:]
        final_url = await self._resolve_b23(code)
        m = _BV_RE.search(final_url)
        if m:
            return VideoTarget(m.group(1), _page_from_url(final_url) or target.page)
        m = _AV_RE.search(final_url)
        if m:
            return VideoTarget(f"av{m.group(1)}", target.page)
        # 异常消息不回显重定向 URL（防半盲 SSRF 探测回显），详情留给日志侧
        raise ValueError("短链未指向B站视频")

    # -- 业务接口 --

    async def search_by_title(self, keyword: str) -> VideoTarget | None:
        """标题关键词搜索 → 取首个视频结果的 VideoTarget。

        用于 QQ 小程序卡片丢 json 载荷时的兜底：卡片文本只剩标题，
        用标题反查 B 站搜索接口拿回视频。需要 WBI 签名。
        失败原因写入 self.last_search_error（None=成功或未执行）。
        """
        kw = (keyword or "").strip()
        self.last_search_error = None
        if not kw:
            self.last_search_error = "空关键词"
            return None
        try:
            mixin_key = await self._ensure_mixin_key()
        except Exception as e:
            self.last_search_error = f"WBI mixin 获取失败: {type(e).__name__}"
            return None
        params = _wbi_sign({
            "search_type": "video",
            "keyword": kw[:80],
            "page": 1,
            "page_size": 5,
        }, mixin_key)
        try:
            data = await self._get_json(
                f"{API_BASE}/x/web-interface/search/type", params)
        except Exception as e:
            # httpx.HTTPStatusError 的 412 = 未带 cookie 被风控，最常见
            self.last_search_error = redact_secrets(
                f"搜索接口异常: {type(e).__name__}: {str(e)[:120]}")
            return None
        if data.get("code") != 0:
            self.last_search_error = (
                f"搜索接口返回 code={data.get('code')} msg={data.get('message')}")
            return None
        for item in (data.get("data") or {}).get("result") or []:
            # result 里的 type 字段区分视频/直播/用户等，只收视频
            if str(item.get("type") or "") != "video":
                continue
            bvid = str(item.get("bvid") or "").strip()
            if bvid.startswith("BV") and len(bvid) == 12:
                return VideoTarget(bvid)
        self.last_search_error = "接口正常但无视频结果（冷门/下架/标题偏差）"
        return None

    async def get_video_info(self, target: VideoTarget) -> dict[str, Any]:
        """L3 基础信息。返回 dict：

        { bvid, aid, cid, title, desc, duration, owner, view, danmaku,
          reply, favorite, coin, share, like, pages, page, tags? }
        pages 为分 P 总数，page 为当前页。
        """
        vid = target.video_id
        params = {"bvid": vid} if vid.startswith("BV") else {"aid": vid[2:]}
        data = await self._get_json(f"{API_BASE}/x/web-interface/view", params)
        if data.get("code") != 0:
            raise ValueError(f"view 接口失败: {data.get('code')} {data.get('message')}")
        d = data.get("data") or {}
        pages = d.get("videos") or 1
        page = min(target.page, pages)
        cid = d.get("cid") or 0
        if pages > 1:
            for p in d.get("pages") or []:
                if int(p.get("page") or 0) == page:
                    cid = p.get("cid") or cid
                    break
        stat = d.get("stat") or {}
        owner = d.get("owner") or {}
        return {
            "bvid": d.get("bvid") or (vid if vid.startswith("BV") else ""),
            "aid": d.get("aid") or (int(vid[2:]) if vid.startswith("av") else 0),
            "cid": cid,
            "title": str(d.get("title") or ""),
            "desc": str(d.get("desc") or ""),
            "duration": int(d.get("duration") or 0),
            "owner": str(owner.get("name") or ""),
            "owner_mid": int(owner.get("mid") or 0),
            "view": int(stat.get("view") or 0),
            "danmaku": int(stat.get("danmaku") or 0),
            "reply": int(stat.get("reply") or 0),
            "like": int(stat.get("like") or 0),
            "coin": int(stat.get("coin") or 0),
            "favorite": int(stat.get("favorite") or 0),
            "pages": pages,
            "page": page,
        }

    async def get_ai_conclusion(self, info: dict[str, Any]) -> dict[str, Any] | None:
        """L1 官方 AI 总结。返回 {summary, outline} 或 None（无总结/失败）。

        接口: /x/web-interface/view/conclusion/get —— WBI 签名 + SESSDATA
        双必需（限制游客访问）。失败原因写入 self.last_conclusion_error。
        """
        self.last_conclusion_error = None
        if not info.get("cid") or not info.get("bvid"):
            self.last_conclusion_error = "view 缺少 cid/bvid"
            return None
        try:
            mixin_key = await self._ensure_mixin_key()
            params = _wbi_sign({
                "bvid": info["bvid"],
                "cid": info["cid"],
                "up_mid": info.get("owner_mid") or 0,
            }, mixin_key)
            data = await self._get_json(
                f"{API_BASE}/x/web-interface/view/conclusion/get", params
            )
        except Exception as e:
            self.last_conclusion_error = redact_secrets(
                f"conclusion 请求异常: {type(e).__name__}: {str(e)[:120]}")
            return None
        if data.get("code") != 0:
            # -403 权限不足 = 未带/带错 SESSDATA；-400 请求错误
            self.last_conclusion_error = (
                f"conclusion code={data.get('code')} msg={data.get('message')}")
            return None
        d = data.get("data") or {}
        # data.code: 0=有摘要, 1=无摘要(未识别到语音), -1=不支持(敏感内容等)
        if d.get("code") not in (0, None):
            self.last_conclusion_error = (
                f"data.code={d.get('code')}（1=未识别到语音, -1=不支持AI摘要）")
            return None
        model_result = d.get("model_result") or {}
        summary = str(model_result.get("summary") or "").strip()
        outline = model_result.get("outline") or []
        if not summary and not outline:
            self.last_conclusion_error = "model_result 为空（无摘要内容）"
            return None
        # outline: [{title, part_outline: [{timestamp, content}]}]
        return {"summary": summary, "outline": outline}

    async def get_subtitle(self, info: dict[str, Any]) -> str | None:
        """L2a CC 字幕全文。需要 WBI 签名；无字幕返回 None。"""
        if not info.get("cid") or not info.get("bvid"):
            return None
        try:
            mixin_key = await self._ensure_mixin_key()
            params = _wbi_sign(
                {"bvid": info["bvid"], "cid": info["cid"], "qn": "64"}, mixin_key
            )
            data = await self._get_json(f"{API_BASE}/x/player/wbi/v2", params)
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        subtitle = ((data.get("data") or {}).get("subtitle") or {})
        subs = subtitle.get("subtitles") or []
        if not subs:
            return None
        # 优先中文字幕，否则第一条
        chosen = next((s for s in subs if str(s.get("lan", "")).startswith("zh")), subs[0])
        sub_url = str(chosen.get("subtitle_url") or "")
        if not sub_url:
            return None
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        if not _is_media_host(sub_url):
            return None  # 字幕 URL 不在 B 站媒体域白名单内，放弃（安全 F-001）
        try:
            resp = await self._media_get(sub_url)
            if resp is None:
                return None
            try:
                resp.raise_for_status()
                body = resp.json()
            finally:
                await resp.aclose()
        except Exception:
            return None
        lines = [str(item.get("content") or "") for item in body.get("body") or []]
        text = " ".join(x for x in lines if x).strip()
        return text or None

    async def get_videoshot(self, info: dict[str, Any]) -> dict[str, Any] | None:
        """L2b 雪碧图元数据。返回 {image_urls, img_x_len, img_y_len, img_x_size, img_y_size, image}。"""
        if not info.get("cid") or not info.get("bvid"):
            return None
        params = {
            "bvid": info["bvid"],
            "cid": info["cid"],
            "index": 1,
        }
        try:
            data = await self._get_json(f"{API_BASE}/x/player/videoshot", params)
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        d = data.get("data") or {}
        urls = d.get("image") or []
        if not urls:
            return None
        return {
            "image_urls": urls,
            "img_x_len": int(d.get("img_x_len") or 10),
            "img_y_len": int(d.get("img_y_len") or 10),
            "img_x_size": int(d.get("img_x_size") or 160),
            "img_y_size": int(d.get("img_y_size") or 90),
        }

    async def download_image(self, url: str) -> bytes | None:
        """下载雪碧图（仅限 B 站媒体域白名单 + 大小上限 + 逐跳白名单）。

        走**无 cookie** 专用 client，且手动跟随重定向（每跳校验白名单）：
        媒体 CDN 跳转不能成为凭据外泄或 SSRF 的出口。
        """
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        client = await self._ensure_media_client()
        for _hop in range(_MEDIA_MAX_HOPS):
            if not _is_media_host(url):
                return None
            try:
                req = client.build_request("GET", url)
                resp = await client.send(req, stream=True)
            except Exception:
                return None
            if resp.status_code in _REDIRECT_CODES:
                loc = resp.headers.get("location") or ""
                await resp.aclose()
                if not loc:
                    return None
                url = urljoin(url, loc)
                continue
            try:
                if not resp.is_success:
                    return None
                length = resp.headers.get("content-length")
                if length and int(length) > _MAX_IMAGE_BYTES:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
            except Exception:
                return None
            finally:
                await resp.aclose()
        return None


def format_count(n: int) -> str:
    """1.2万 / 3.4亿 风格数字。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def format_duration(sec: int) -> str:
    """时长 → mm:ss 或 h:mm:ss。"""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
