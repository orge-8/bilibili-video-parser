"""关键帧识别（org.mai-mai.bilibili-video-parser）。

官方雪碧图 → Pillow 均匀抽帧 → 一次多图 VLM 调用识别 → 一次文本 LLM 调用合成总结。

VLM/LLM 调用模式复用 qzone-feeds vision.py 的双层超时：
  - 外层 asyncio.wait_for 总闸
  - 内层 call_capability("llm.generate", timeout_ms=...) 覆盖 SDK 默认 30s RPC 超时
"""
import asyncio
import base64
import io

# Pillow 缺失时优雅降级（manifest 已声明，正常环境必装）
try:
    from PIL import Image
    _PIL_OK = True
except Exception:  # pragma: no cover
    Image = None
    _PIL_OK = False


class NoLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass


logger = NoLogger()


def set_frame_logger(custom_logger):
    global logger
    logger = custom_logger


# 单帧识别（多图打包一次调用）超时
VISION_TIMEOUT_SEC = 90
VISION_RPC_TIMEOUT_MS = 85_000
# 帧描述合成总结超时
SUMMARY_TIMEOUT_SEC = 60
SUMMARY_RPC_TIMEOUT_MS = 55_000
# 单帧子图最长边（雪碧图格子本身不大，按原格尺寸发即可；超 512 再压）
FRAME_MAX_EDGE = 512
JPEG_QUALITY = 85

_FRAME_DESC_PROMPT = (
    "以下是同一个B站视频按时间顺序抽取的{n}张关键帧（每张按顺序对应视频的不同时间点）。"
    "请按帧编号逐条简述每帧内容（画面主体/场景/字幕要点/进度变化），每条不超过{max_chars}字，"
    "不要评价，不要输出与帧无关的内容。"
)

_SUMMARY_PROMPT = (
    "以下是一个B站视频的关键帧内容描述（按时间顺序）。请综合这些关键帧，"
    "用不超过200字总结这个视频讲了什么内容、大致的时间脉络。直接输出总结正文，"
    "不要前缀、不要评价。\n\n视频标题：{title}\nUP主：{owner}\n\n关键帧描述：\n{frames}"
)


def _extract_frames(sprite_bytes: bytes, shot_meta: dict, max_frames: int) -> list[bytes]:
    """从雪碧图按网格均匀抽帧，返回 JPEG bytes 列表。

    shot_meta: bili_video.get_videoshot 的返回（img_x_len/img_y_len/img_x_size/img_y_size）。
    """
    if not _PIL_OK:
        raise RuntimeError("Pillow 未安装，无法切帧")
    img = Image.open(io.BytesIO(sprite_bytes))
    x_len = max(1, int(shot_meta.get("img_x_len") or 10))
    y_len = max(1, int(shot_meta.get("img_y_len") or 10))
    x_size = int(shot_meta.get("img_x_size") or 160)
    y_size = int(shot_meta.get("img_y_size") or 90)
    # 实际格子尺寸以图为准确认（hdslb 有时返回旧参数）
    cell_w = img.width // x_len if img.width >= x_len else x_size
    cell_h = img.height // y_len if img.height >= y_len else y_size
    total_cells = x_len * y_len
    if total_cells <= 0 or cell_w <= 0 or cell_h <= 0:
        raise RuntimeError(f"雪碧图网格参数异常: {shot_meta}")
    n = min(max_frames, total_cells)
    # 均匀采样格子索引（首尾必含）
    if n <= 1:
        indices = [0]
    else:
        indices = [round(i * (total_cells - 1) / (n - 1)) for i in range(n)]
    frames: list[bytes] = []
    for idx in indices:
        row, col = divmod(int(idx), x_len)
        box = (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
        if box[2] > img.width or box[3] > img.height:
            continue
        cell = img.crop(box)
        # 格子太大时压缩
        if max(cell.width, cell.height) > FRAME_MAX_EDGE:
            scale = FRAME_MAX_EDGE / max(cell.width, cell.height)
            cell = cell.resize((max(1, round(cell.width * scale)),
                                max(1, round(cell.height * scale))))
        buf = io.BytesIO()
        cell.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
        frames.append(buf.getvalue())
    return frames


def _frames_to_data_urls(frames: list[bytes]) -> list[str]:
    return [f"data:image/jpeg;base64,{base64.b64encode(f).decode('ascii')}" for f in frames]


class FrameVisionManager:
    """关键帧 VLM 识别 + 宿主总结。任何失败返回 None（链路继续降级）。"""

    def __init__(self, plugin):
        # plugin 提供 self.ctx / self.config / self._resolve_llm_params
        self._plugin = plugin

    async def analyze(self, info: dict, shot_meta: dict,
                      client, sprite_urls: list[str]) -> str | None:
        """下载雪碧图 → 抽帧 → VLM 描述 → LLM 总结。返回总结文本或 None。

        client: bili_video.BiliVideoClient（复用其 download_image）。
        """
        cfg = self._plugin.config.parse
        max_frames = max(1, min(int(cfg.max_frames or 4), 9))
        if not sprite_urls:
            return None
        sprite_bytes = await client.download_image(sprite_urls[0])
        if not sprite_bytes:
            logger.warning(f"雪碧图下载失败: {sprite_urls[0][:100]}")
            return None
        try:
            frames = _extract_frames(sprite_bytes, shot_meta, max_frames)
        except Exception as e:
            logger.warning(f"雪碧图切帧失败: {e}")
            return None
        if not frames:
            logger.warning("雪碧图切帧结果为空")
            return None
        desc_max = max(40, min(int(cfg.desc_max_chars or 120), 300))
        frame_desc = await self._describe_frames(frames, desc_max)
        if not frame_desc:
            return None
        summary = await self._summarize(info, frame_desc)
        return summary

    async def _call_llm(self, prompt, llm_kwargs: dict, timeout_sec: float,
                        rpc_timeout_ms: int):
        """双层超时的 llm.generate（外层 wait_for + 内层 timeout_ms 覆盖 RPC 默认）。"""
        ctx = self._plugin.ctx
        call_capability = getattr(ctx, "call_capability", None)
        if callable(call_capability):
            return await asyncio.wait_for(
                call_capability("llm.generate", timeout_ms=rpc_timeout_ms,
                                prompt=prompt, **llm_kwargs),
                timeout=timeout_sec,
            )
        # 老 SDK 兜底
        return await asyncio.wait_for(
            ctx.llm.generate(prompt=prompt, **llm_kwargs),
            timeout=timeout_sec,
        )

    async def _describe_frames(self, frames: list[bytes], desc_max: int) -> str | None:
        """一次多图调用生成帧描述。"""
        llm_kwargs = self._plugin._resolve_vision_params()
        if not llm_kwargs and not self._plugin._vision_fallback_ok():
            logger.warning("视觉任务/模型均未配置，跳过关键帧识别")
            return None
        data_urls = _frames_to_data_urls(frames)
        content = [{"type": "text",
                    "text": _FRAME_DESC_PROMPT.format(n=len(data_urls), max_chars=desc_max)}]
        for url in data_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        prompt = [{"role": "user", "content": content}]
        try:
            result = await self._call_llm(prompt, llm_kwargs,
                                          VISION_TIMEOUT_SEC, VISION_RPC_TIMEOUT_MS)
        except asyncio.TimeoutError:
            logger.warning(f"关键帧识别超时（>{VISION_TIMEOUT_SEC}s）")
            return None
        except Exception as e:
            logger.warning(f"关键帧识别失败: {e}")
            return None
        text = self._extract_text(result)
        if not text:
            logger.warning("关键帧识别返回空文本")
            return None
        return text

    async def _summarize(self, info: dict, frame_desc: str) -> str | None:
        """帧描述 → 宿主文本模型总结。"""
        llm_kwargs = self._plugin._resolve_summary_params()
        prompt = _SUMMARY_PROMPT.format(
            title=info.get("title") or "",
            owner=info.get("owner") or "",
            frames=frame_desc[:4000],
        )
        try:
            result = await self._call_llm(prompt, llm_kwargs,
                                          SUMMARY_TIMEOUT_SEC, SUMMARY_RPC_TIMEOUT_MS)
        except asyncio.TimeoutError:
            logger.warning(f"关键帧总结超时（>{SUMMARY_TIMEOUT_SEC}s）")
            return None
        except Exception as e:
            logger.warning(f"关键帧总结失败: {e}")
            return None
        return self._extract_text(result)

    @staticmethod
    def _extract_text(result) -> str | None:
        """从 llm.generate 响应提取正文（兼容多种字段形态）。"""
        if not isinstance(result, dict):
            return None
        for key in ("text", "content", "result", "output"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None
