"""org.mai-mai.bilibili-video-parser —— B站视频解析插件（v1.0.0）

自动识别聊天中的 B 站视频（BV/av 号、bilibili.com 链接、b23.tv 短链），解析视频内容
并注入消息上下文（改写 processed_plain_text，不发冗余卡片），供 bot 理解讨论；
另提供 /bili 命令与 parse_bilibili_video 工具显式查询。

三级降级链：
  L1 官方 AI 总结（view/conclusion，SESSDATA 可选）
  L2 关键帧 VLM 识别+宿主总结（雪碧图，无需 ffmpeg）／ CC 字幕 ／ 简介+tag（并列）
  L3 纯基础信息（view 接口，标题/UP/时长/数据）

参考：YukiSakiko/content_understanding_plugin（hook 注入形态）、
Mettafy/bilibili_video_parser（降级链与超时预算思想）。
"""
import asyncio
import time
from typing import Any

from maibot_sdk import (
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import HookMode, HookOrder, ErrorPolicy

# Runner 以包形式加载（相对导入可用）；fakehost/pytest 以文件路径加载（走 sys.path）
try:
    from .bili_video import (
        BiliVideoClient,
        VideoTarget,
        extract_targets_from_text,
        format_count,
        format_duration,
        parse_explicit_target,
    )
    from .frame_vision import FrameVisionManager, set_frame_logger
except ImportError:
    from bili_video import (
        BiliVideoClient,
        VideoTarget,
        extract_targets_from_text,
        format_count,
        format_duration,
        parse_explicit_target,
    )
    from frame_vision import FrameVisionManager, set_frame_logger

PLUGIN_VERSION = "1.0.0"

# 注入块头部（bot 可读标记）
_INJECT_HEADER = "\n\n[B站视频解析]"
# 注入文本总长上限（防上下文膨胀）
_INJECT_MAX_CHARS = 1200
# L1 总结正文字符上限
_SUMMARY_MAX_CHARS = 500
# L2 字幕拼接字符上限
_SUBTITLE_MAX_CHARS = 1500


# ── 配置模型 ──────────────────────────────────────────────────

class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: float = Field(default=1.0, description="配置版本号（Host 版本策略必填）")
    summary_task: str = Field(
        default="", description="L2 关键帧总结的宿主任务名（留空走 SDK 默认 utils）")
    vision_task: str = Field(
        default="", description="关键帧识别的宿主视觉任务名（留空走 SDK 默认）")
    vision_model_name: str = Field(
        default="", description="视觉模型名（覆盖任务，须在 model_config.toml 中存在）")


class ParseSectionConfig(PluginConfigBase):
    enable_in_group: bool = Field(default=True, description="群聊自动检测注入")
    enable_in_private: bool = Field(default=True, description="私聊自动检测注入")
    cache_ttl_seconds: int = Field(default=1800, description="视频解析缓存时长（秒）")
    request_timeout_sec: float = Field(default=15.0, description="B站 API 单请求超时（秒）")
    enable_ai_summary: bool = Field(default=True, description="启用 L1 官方 AI 总结")
    enable_frame_vision: bool = Field(default=True, description="启用 L2 关键帧识别（雪碧图+VLM）")
    enable_subtitle: bool = Field(default=True, description="启用 L2 CC 字幕")
    max_frames: int = Field(default=4, description="关键帧抽取张数（1~9）")
    desc_max_chars: int = Field(default=120, description="单帧描述字符上限（40~300）")
    min_video_duration_sec: int = Field(
        default=60, description="低于该时长（秒）跳过关键帧（短视频官方总结通常已有）")


class CredentialSectionConfig(PluginConfigBase):
    sessdata: str = Field(default="", description="B站 SESSDATA（可选，提升 AI 总结命中率）")


class TriggerSectionConfig(PluginConfigBase):
    hook_total_timeout_sec: float = Field(
        default=25.0, description="hook 自动检测链总预算（秒），超时只注入已有结果")
    command_total_timeout_sec: float = Field(
        default=150.0, description="/bili 命令链总预算（秒）")


class BiliParserConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    parse: ParseSectionConfig = Field(default_factory=ParseSectionConfig)
    credential: CredentialSectionConfig = Field(default_factory=CredentialSectionConfig)
    trigger: TriggerSectionConfig = Field(default_factory=TriggerSectionConfig)


# ── 插件主体 ──────────────────────────────────────────────────

class BilibiliVideoParserPlugin(MaiBotPlugin):
    """B站视频解析：hook 自动注入 + /bili 命令 + LLM 工具，三级降级。"""

    config_model = BiliParserConfig

    def __init__(self):
        super().__init__()
        self._client: BiliVideoClient | None = None
        self._vision: FrameVisionManager | None = None
        self._cache: dict[str, tuple[float, str]] = {}  # cache_key -> (ts, 注入文本)
        self._seen_messages: dict[str, float] = {}  # message_id -> ts（去重）

    # ---------- 生命周期 ----------

    async def on_load(self):
        logger = self.ctx.logger
        set_frame_logger(logger)
        self._client = BiliVideoClient(
            sessdata=self.config.credential.sessdata,
            timeout_sec=float(self.config.parse.request_timeout_sec or 15.0),
        )
        self._vision = FrameVisionManager(self)
        self._cache.clear()
        self._seen_messages.clear()
        logger.info(f"bilibili-video-parser v{PLUGIN_VERSION} 已加载")

    async def on_unload(self):
        # 清理在途缓存与 httpx client
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        self._vision = None
        self._cache.clear()
        self._seen_messages.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str):
        # Runner 推送新配置后重建 client（SESSDATA / 超时可能变化）
        if self._client is not None:
            self._client.update_sessdata(self.config.credential.sessdata)

    # ---------- llm.generate 参数解析（1.2.5 task/model 语义） ----------

    def _resolve_vision_params(self) -> dict:
        p = self.config.plugin
        kwargs: dict[str, Any] = {}
        if str(p.vision_task or "").strip():
            kwargs["task_name"] = str(p.vision_task).strip()
        if str(p.vision_model_name or "").strip():
            kwargs["model"] = str(p.vision_model_name).strip()
        return kwargs

    def _vision_fallback_ok(self) -> bool:
        """视觉参数为空时是否允许走 SDK 默认任务（默认 True，走 utils 视觉池）。"""
        return True

    def _resolve_summary_params(self) -> dict:
        p = self.config.plugin
        kwargs: dict[str, Any] = {}
        if str(p.summary_task or "").strip():
            kwargs["task_name"] = str(p.summary_task).strip()
        return kwargs

    # ---------- 降级链 ----------

    def _cache_get(self, target: VideoTarget) -> str | None:
        entry = self._cache.get(target.cache_key)
        if not entry:
            return None
        ts, text = entry
        if time.time() - ts > int(self.config.parse.cache_ttl_seconds or 1800):
            self._cache.pop(target.cache_key, None)
            return None
        return text

    def _cache_put(self, target: VideoTarget, text: str) -> None:
        self._cache[target.cache_key] = (time.time(), text)
        # 有界缓存（LRU 粗剪）
        limit = 200
        if len(self._cache) > limit:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0]):
                self._cache.pop(k, None)
                if len(self._cache) <= limit:
                    break

    async def _resolve_video(self, target: VideoTarget,
                             total_timeout_sec: float | None = None) -> tuple[str, str]:
        """跑降级链。返回 (注入文本, 来源级别标记)。

        级别标记：ai_summary / frame_vision / subtitle / desc / basic / error
        """
        logger = self.ctx.logger
        cached = self._cache_get(target)
        if cached:
            return cached, "cache"
        assert self._client is not None

        budget: float | None = None
        if total_timeout_sec and total_timeout_sec > 0:
            deadline = time.monotonic() + total_timeout_sec

        def _left() -> float | None:
            if total_timeout_sec is None or total_timeout_sec <= 0:
                return None
            return deadline - time.monotonic()

        # 短链解析
        real_target = await self._client.resolve_target(target)
        info = await self._client.get_video_info(real_target)
        basic_block = self._format_basic(info)

        # L1 官方 AI 总结
        if self.config.parse.enable_ai_summary and self._within_budget(_left(), 8.0):
            conclusion = await self._client.get_ai_conclusion(info)
            if conclusion:
                block = basic_block + "\n" + self._format_ai_summary(conclusion)
                text = self._clip(_INJECT_HEADER + "\n" + block)
                self._cache_put(real_target, text)
                logger.info(f"视频解析命中 L1 官方总结: {real_target.video_id}")
                return text, "ai_summary"
            logger.info(f"官方 AI 总结不可用，降级 L2: {real_target.video_id}")

        # L2 关键帧（雪碧图）优先，其次字幕，最后简介
        if (self.config.parse.enable_frame_vision
                and info.get("duration", 0) >= int(self.config.parse.min_video_duration_sec or 60)
                and self._within_budget(_left(), 30.0)):
            shot = await self._client.get_videoshot(info)
            if shot and self._vision is not None:
                try:
                    summary = await self._vision.analyze(
                        info, shot, self._client, shot.get("image_urls") or [])
                except Exception as e:
                    logger.warning(f"关键帧识别异常: {e}")
                    summary = None
                if summary:
                    block = basic_block + f"\n[关键帧总结] {summary}"
                    text = self._clip(_INJECT_HEADER + "\n" + block)
                    self._cache_put(real_target, text)
                    logger.info(f"视频解析命中 L2 关键帧: {real_target.video_id}")
                    return text, "frame_vision"
                logger.info(f"关键帧识别未产出结果，尝试字幕: {real_target.video_id}")

        if self.config.parse.enable_subtitle and self._within_budget(_left(), 10.0):
            subtitle = await self._client.get_subtitle(info)
            if subtitle:
                sub_clip = subtitle[:_SUBTITLE_MAX_CHARS]
                block = basic_block + f"\n[字幕节选] {sub_clip}"
                text = self._clip(_INJECT_HEADER + "\n" + block)
                self._cache_put(real_target, text)
                logger.info(f"视频解析命中 L2 字幕: {real_target.video_id}")
                return text, "subtitle"

        # L2c 简介 + L3
        desc = str(info.get("desc") or "").strip()
        if desc and desc != "-":
            block = basic_block + f"\n[简介] {desc[:400]}"
            text = self._clip(_INJECT_HEADER + "\n" + block)
            self._cache_put(real_target, text)
            logger.info(f"视频解析命中 L2 简介: {real_target.video_id}")
            return text, "desc"

        text = self._clip(_INJECT_HEADER + "\n" + basic_block)
        self._cache_put(real_target, text)
        logger.info(f"视频解析命中 L3 基础信息: {real_target.video_id}")
        return text, "basic"

    @staticmethod
    def _within_budget(left: float | None, need: float) -> bool:
        return left is None or left >= need

    @staticmethod
    def _clip(text: str) -> str:
        limit = _INJECT_MAX_CHARS
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @staticmethod
    def _format_basic(info: dict) -> str:
        lines = [
            f"标题：{info.get('title') or '（无）'}",
            f"UP主：{info.get('owner') or '（无）'}",
            f"时长：{format_duration(int(info.get('duration') or 0))}",
            f"数据：播放 {format_count(int(info.get('view') or 0))} / "
            f"弹幕 {format_count(int(info.get('danmaku') or 0))} / "
            f"点赞 {format_count(int(info.get('like') or 0))} / "
            f"投币 {format_count(int(info.get('coin') or 0))}",
        ]
        if int(info.get("pages") or 1) > 1:
            lines.append(f"分P：{info.get('page')}/{info.get('pages')}")
        return "\n".join(lines)

    @staticmethod
    def _format_ai_summary(conclusion: dict) -> str:
        parts = []
        summary = str(conclusion.get("summary") or "").strip()
        if summary:
            parts.append(f"[AI总结] {summary[:_SUMMARY_MAX_CHARS]}")
        outline = conclusion.get("outline") or []
        if outline and len(parts) == 1:
            chapters = []
            for chapter in outline[:5]:
                title = str(chapter.get("title") or "").strip()
                if title:
                    chapters.append(title)
            if chapters:
                parts.append("[章节] " + "；".join(chapters))
        return "\n".join(parts)

    # ---------- Hook：自动检测 + 上下文注入 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="bili_video_detect",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_incoming_message(self, **kwargs: Any):
        """入站消息后处理：检测 B 站视频 → 解析 → 改写 processed_plain_text 注入。"""
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        message = kwargs.get("message")
        if not isinstance(message, dict) or not message:
            return {"action": "continue"}

        # 群/私聊开关（group_id 平铺与 group_info 嵌套两形态）
        if not self._chat_allowed(message, kwargs):
            return {"action": "continue"}

        text = str(message.get("processed_plain_text") or "")
        if not text or text.startswith("/"):
            return {"action": "continue"}
        # raw 段兜底（json 分享卡片里可能有链接文本）
        if not _has_target_hint(text):
            raw = message.get("raw_message")
            if isinstance(raw, list):
                for seg in raw:
                    if isinstance(seg, dict) and _has_target_hint(
                            str(seg.get("data") or "")):
                        text = text + " " + str(seg.get("data") or "")
                        break

        hits = extract_targets_from_text(text)
        if not hits:
            return {"action": "continue"}

        # message_id 去重
        message_id = str(message.get("message_id") or "")
        now = time.time()
        if message_id:
            if message_id in self._seen_messages:
                return {"action": "continue"}
            self._seen_messages[message_id] = now
            if len(self._seen_messages) > 512:
                self._seen_messages = {
                    k: v for k, v in self._seen_messages.items() if now - v < 300
                }

        budget = float(self.config.trigger.hook_total_timeout_sec or 25.0)
        inject_blocks: list[str] = []
        for frag, _kind in hits[:2]:  # 单条消息最多解析 2 个视频
            target = parse_explicit_target(frag)
            if target is None:
                continue
            left = budget - 0.0
            try:
                block_text, level = await asyncio.wait_for(
                    self._resolve_video(target, total_timeout_sec=max(1.0, left)),
                    timeout=max(1.0, left),
                )
                inject_blocks.append(block_text)
            except asyncio.TimeoutError:
                # 总预算耗尽：至少留下标记行，供 bot 知道这是一个B站视频
                inject_blocks.append(_INJECT_HEADER + "（解析超时，内容未获取）")
                self.ctx.logger.warning(f"hook 解析超时: {target.video_id}")
                break
            except Exception as e:
                inject_blocks.append(_INJECT_HEADER + f"（解析失败：{e}）")
                self.ctx.logger.warning(f"hook 解析失败: {target.video_id}: {e}")
                continue
            budget = max(0.0, budget - (time.time() - (now or time.time())))

        if not inject_blocks:
            return {"action": "continue"}

        # 改写消息文本，注入解析块（bot 查上下文即可理解视频）
        merged = "\n".join(inject_blocks)
        current = str(message.get("processed_plain_text") or "")
        message["processed_plain_text"] = current + merged
        if isinstance(message.get("text"), str) and message["text"]:
            message["text"] = message["text"] + merged
        return {"action": "continue", "modified_kwargs": kwargs}

    def _chat_allowed(self, message: dict, kwargs: dict) -> bool:
        cfg = self.config.parse
        mi = message.get("message_info")
        is_group = bool(kwargs.get("is_group"))
        if isinstance(mi, dict):
            gi = mi.get("group_info")
            if mi.get("group_id") or (isinstance(gi, dict) and gi.get("group_id")):
                is_group = True
        return cfg.enable_in_group if is_group else cfg.enable_in_private

    # ---------- Command：/bili <目标> ----------

    @Command("bili_parse", pattern=r"^\s*[/／]\s*bili\s+(?P<target>.+?)\s*$")
    async def cmd_bili(self, **kwargs):
        stream_id = kwargs.get("stream_id")
        raw = str(kwargs.get("matched_groups", {}).get("target") or "").strip()
        if not raw:
            await self.ctx.send.text("用法：/bili <BV号/av号/链接>", stream_id)
            return False, "参数为空", 1
        target = parse_explicit_target(raw)
        if target is None:
            await self.ctx.send.text(
                "无法识别视频目标，支持：BV号 / av号 / bilibili.com 链接 / b23.tv 短链", stream_id)
            return False, "目标无法解析", 1
        budget = float(self.config.trigger.command_total_timeout_sec or 150.0)
        try:
            text, level = await asyncio.wait_for(
                self._resolve_video(target, total_timeout_sec=budget),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            await self.ctx.send.text("解析超时，请稍后再试", stream_id)
            return False, "解析超时", 2
        except Exception as e:
            await self.ctx.send.text(f"解析失败：{e}", stream_id)
            return False, f"解析失败: {e}", 2
        # 命令场景去掉注入头，直接输出正文
        body = text.replace(_INJECT_HEADER, "", 1).strip()
        await self.ctx.send.text(body, stream_id)
        return True, level, 0

    # ---------- Tool：LLM 显式调用 ----------

    @Tool(
        "parse_bilibili_video",
        description="解析B站视频：获取标题、UP主、时长、数据，以及AI总结/关键帧/字幕/简介内容",
        parameters={
            "video": {
                "type": "string",
                "description": "BV号、av号、视频链接或 b23.tv 短链",
            },
        },
    )
    async def parse_bilibili_video(self, video: str = "", **kwargs):
        raw = str(video or "").strip()
        if not raw:
            return "参数 video 不能为空：请传入 BV号/av号/链接"
        target = parse_explicit_target(raw)
        if target is None:
            return "无法识别视频目标，支持：BV号 / av号 / bilibili.com 链接 / b23.tv 短链"
        try:
            text, _level = await self._resolve_video(
                target, total_timeout_sec=float(self.config.trigger.command_total_timeout_sec or 150.0))
            return text.replace(_INJECT_HEADER, "", 1).strip()
        except Exception as e:
            return f"解析失败：{e}"


def _has_target_hint(text: str) -> bool:
    """轻量判断：文本里是否有 B 站视频痕迹（避免对普通消息跑完整正则）。"""
    low = text.lower()
    return ("b23.tv" in low or "bilibili.com" in low
            or "bv1" in low or "bv2" in low or "bv3" in low
            or "av1" in low or "av2" in low or "av3" in low)


def create_plugin() -> BilibiliVideoParserPlugin:
    return BilibiliVideoParserPlugin()
