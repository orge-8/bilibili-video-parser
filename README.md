# bilibili-video-parser（B站视频解析）

MaiBot 插件 `org.mai-mai.bilibili-video-parser` v1.0.0。自动识别聊天中的 B 站视频
（BV/av 号、bilibili.com 链接、b23.tv 短链），解析内容并注入消息上下文供 bot 理解讨论；
另提供 `/bili` 命令与 `parse_bilibili_video` 工具。

参考：[YukiSakiko/content_understanding_plugin](https://github.com/YukiSakiko/content_understanding_plugin)
（hook 注入形态）、[Mettafy/bilibili_video_parser](https://github.com/Mettafy/bilibili_video_parser)
（降级链思想）。

## 功能

- **自动检测（上下文注入，不刷屏）**：群聊/私聊出现 B 站视频链接时，自动解析并把
  结果追加到该消息的 `processed_plain_text`，bot 查看上下文即可理解视频内容并参与讨论，
  不会额外发送卡片消息。
- **`/bili <目标>` 命令**：显式解析，直接返回完整结果文本。
- **`parse_bilibili_video` 工具**：供 Planner/Replyer 按需调用。
- 单条消息最多解析 2 个视频；同一视频 30 分钟内缓存复用。

## 三级降级链

| 级别 | 内容 | 条件 |
|---|---|---|
| L1 | B 站官方 AI 总结 + 章节 | 视频已有官方总结（配 SESSDATA 命中率更高） |
| L2 | 关键帧 VLM 识别 + 宿主总结（官方雪碧图切帧，无需 ffmpeg）／ CC 字幕节选 ／ 简介文本 | 按可用性依次尝试；关键帧需视频 ≥60s 且已配置视觉任务 |
| L3 | 纯基础信息（标题/UP主/时长/播放/弹幕/点赞/投币/分P） | 始终可用 |

每次解析的命中级别会写入插件日志，注入文本头部带来源标记（`[AI总结]`/`[关键帧总结]`/`[字幕节选]`/`[简介]`）。

## 安装

```bash
# 依赖已在 _manifest.json 声明（httpx、pillow），Host 会自动安装；
# 手动安装：
pip install httpx pillow
```

把 `bilibili-video-parser/` 整目录放入 MaiBot `plugins/`，**完整重启 MaiBot**
（manifest 变更热重载不生效）。

## 配置

无需手动创建 `config.toml`，Runner 首次启动自动生成，WebUI 可视化编辑。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | true | 插件总开关 |
| `plugin.summary_task` | "" | 关键帧总结宿主任务名（留空走默认 utils） |
| `plugin.vision_task` | "" | 关键帧识别宿主视觉任务名（留空走默认） |
| `plugin.vision_model_name` | "" | 视觉模型名（覆盖任务，须在 model_config.toml） |
| `parse.enable_in_group` / `enable_in_private` | true | 群聊/私聊自动检测开关 |
| `parse.cache_ttl_seconds` | 1800 | 解析缓存时长 |
| `parse.request_timeout_sec` | 15 | B 站 API 单请求超时 |
| `parse.enable_ai_summary` | true | L1 开关 |
| `parse.enable_frame_vision` | true | L2 关键帧开关 |
| `parse.enable_subtitle` | true | L2 字幕开关 |
| `parse.max_frames` | 4 | 关键帧张数（1~9） |
| `parse.min_video_duration_sec` | 60 | 低于该时长跳过关键帧 |
| `credential.sessdata` | "" | B 站 SESSDATA（可选） |
| `trigger.hook_total_timeout_sec` | 25 | 自动检测链总预算 |
| `trigger.command_total_timeout_sec` | 150 | /bili 命令链总预算 |

## 权限/能力

- capabilities：`send.text`（命令回复）、`llm.generate`（L2 关键帧识别与总结）
- 不调用适配器 API，不依赖 napcat-adapter

## 故障排查

| 症状 | 处置 |
|---|---|
| 群里发链接 bot 没反应 | 查日志 `org.mai-mai.bilibili-video-parser`；确认 `parse.enable_in_group`；确认消息文本含可识别目标（日志有 `视频解析命中` 字样即链路正常，注入后是否回复由 bot 主流程决定） |
| 全是 L3 基础信息 | 未配 SESSDATA 时官方总结命中率低；配 `credential.sessdata`（建议小号）。L2 关键帧需宿主已配置视觉任务 |
| 关键帧识别超时 | 调小 `parse.max_frames`；确认视觉模型可用 |
| HTTPS 证书错误（CERTIFICATE_VERIFY_FAILED） | 运行机存在 TLS MITM 代理时会给 httpx 指定信任链，或将 api.bilibili.com / hdslb 域加入代理白名单 |
| 字幕拿不到 | CC 字幕需 UP 主上传；`SESSDATA` 获取字幕可能触发风控（建议小号） |

## 许可证

MIT
