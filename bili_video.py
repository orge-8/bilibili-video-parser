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
from urllib.parse import parse_qs, urlencode, urlparse

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
# 单张图片下载上限（10MB，防解压炸弹/资源耗尽）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

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
        found.append((m.group(1), "b23"))
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
        self._mixin_key = ""
        self._mixin_key_ts = 0.0

    # -- 生命周期 --

    @staticmethod
    def _new_client(timeout_sec: float, cookies: dict | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers=dict(_HEADERS),
            cookies=cookies,
            follow_redirects=True,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            cookies = {"buvid3": self._get_buvid3()}
            if self._sessdata:
                cookies["SESSDATA"] = self._sessdata
            self._client = self._new_client(self._timeout, cookies)
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
            self._shortlink_client = self._new_client(self._timeout, None)
        return self._shortlink_client

    async def close(self) -> None:
        for attr in ("_client", "_shortlink_client"):
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

    def update_sessdata(self, sessdata: str) -> None:
        """更新 SESSDATA。返回 True 表示 client 需要重建（异步重建由调用方触发）。"""
        sd = (sessdata or "").strip()
        if sd != self._sessdata:
            self._sessdata = sd
            self._client = None  # 下一轮 _ensure_client 重建；旧 client 由 close 统一兜底
            self._dirty = True

    def update_timeout(self, timeout_sec: float) -> None:
        """更新超时；下一次 _ensure_client 生效（client 置空触发重建）。"""
        t = max(1.0, float(timeout_sec or 15.0))
        if t != self._timeout:
            self._timeout = t
            self._client = None

    # -- 内部 --

    async def _get_json(self, url: str, params: dict[str, Any] | None = None,
                        extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        client = await self._ensure_client()
        resp = await client.get(url, params=params, headers=extra_headers)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"B站接口返回非对象: {type(data)}")
        return data

    async def _resolve_b23(self, code: str) -> str:
        """b23.tv 短链 → 目标 URL。逐跳跟随并校验域名白名单（防 SSRF）。

        使用无 cookie 专用 client；任一跳落在白名单外即抛 ValueError。
        """
        client = await self._ensure_shortlink_client()
        url = f"https://b23.tv/{code}"
        for _hop in range(_SHORTLINK_MAX_HOPS):
            host = (urlparse(url).hostname or "").lower()
            if host not in _SHORTLINK_HOSTS:
                raise ValueError(f"短链重定向到非白名单域: {host}")
            resp = await client.get(url)
            await resp.aclose()
            next_url = str(resp.url)
            if next_url == url:
                return next_url  # 不再重定向
            url = next_url
        raise ValueError("短链重定向次数过多")

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
            self.last_search_error = f"搜索接口异常: {type(e).__name__}: {str(e)[:120]}"
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
        """L1 官方 AI 总结。返回 {summary, outline} 或 None（无总结/失败）。"""
        if not info.get("cid") or not info.get("bvid"):
            return None
        params = {
            "bvid": info["bvid"],
            "cid": info["cid"],
            "up_mid": info.get("owner_mid") or 0,
        }
        try:
            data = await self._get_json(
                f"{API_BASE}/x/web-interface/view/conclusion", params
            )
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        model_result = ((data.get("data") or {}).get("model_result") or {})
        summary = str(model_result.get("summary") or "").strip()
        outline = model_result.get("outline") or []
        if not summary and not outline:
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
            client = await self._ensure_client()
            resp = await client.get(sub_url)
            resp.raise_for_status()
            body = resp.json()
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
        """下载雪碧图（仅限 B 站媒体域白名单 + 大小上限）。失败返回 None。"""
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        if not _is_media_host(url):
            return None
        try:
            client = await self._ensure_client()
            resp = await client.send(client.build_request("GET", url), stream=True)
            try:
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
            finally:
                await resp.aclose()
        except Exception:
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
