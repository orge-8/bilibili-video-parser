# bilibili-video-parser（B站视频解析）

MaiBot 插件 `org.mai-mai.bilibili-video-parser` v1.0.4。自动识别聊天中的 B 站视频
（BV/av 号、bilibili.com 链接、b23.tv 短链、QQ 小程序分享卡片），解析内容并注入消息
上下文供 bot 理解讨论；另提供 `/bili` 命令与 `parse_bilibili_video` 工具。

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

## v1.0.4 修复：NapCat get_msg 回查（根治方案）

- **参考实现**：[Mettafy/bilibili_video_parser](https://github.com/Mettafy/bilibili_video_parser)
  Maisaka 版 `runtime/napcat_resolver.py`——小程序卡片链接不在 MaiBot 消息体里时，
  调适配器 API `adapter.napcat.message.get_msg` 回查 NapCat 原始消息（json 段完好）。
- **兜底链升级**：卡片无链接时 1) NapCat get_msg 回查 → 深度扫描（字符串自动
  json.loads 展开，dict 按 `jumpUrl`/`qqdocurl`/`meta`/`detail_1`/`miniapp` 等优先键遍历）
  提取目标；2) 通道不可用/未命中 → 回退标题反查（v1.0.3）。
- **通道兼容**：`ctx.api.call`（SDK 2.8.2+）→ `call_capability("api.call")`
  （SDK 2.8.1，Host 支持即可）→ 双通道都失败才回退反查。get_msg 10s 超时。

## v1.0.3 修复：小程序卡片标题反查兜底

- **根因确认**（真机 16:58 日志）：QQ 小程序卡片的 json 载荷在 napcat-adapter → MaiBot
  管线中被剥掉，`raw_message` 只剩 2 个 text 段（标题文本 + 图片描述），b23 关键词不存在——
  插件侧无链接可提取。
- **标题反查兜底**：卡片文本含 `哔哩哔哩：<标题>` 时，用标题调用 B 站搜索接口
  （`search/type`，WBI 签名）取首个视频结果，走既有解析链注入。反查失败/无结果时静默
  （仅日志留痕，不注入）。
- 注入行为与链接解析路径一致（同款缓存、降级链、注入头）。

## v1.0.2 修复：QQ 小程序分享卡片

- **小程序卡片识别**：QQ 哔哩哔哩小程序卡片（ark/json 段）的跳转链接藏在 JSON 字符串里，
  且为转义形态（`https:\/\/b23.tv\/xxx`）。hook 现在会扫描 raw_message 的 str/list/json 段，
  还原 `\/`、`\u002F`、`\u0026` 转义后提取 b23 短链并正常解析注入。
- **诊断埋点**：小程序卡片提取不到目标时输出日志 `小程序卡片未提取到B站目标，原文片段: ...`，
  用于区分「适配器未转发 json 载荷」与「提取失败」。

## v1.0.1 安全与修复

- **SSRF 加固**：b23.tv 短链解析改为手动跟随跳转 + 域名白名单校验（跳转出白名单即拒绝）；
  图片/字幕/雪碧图下载仅允许 B 站媒体域（hdslb.com 等），并限制单图 ≤10MB。
- **注入头防提示注入**：注入文本头部带不可信来源标注（远程视频内容非指令）。
- **hook 预算与门槛解耦**：hook 总预算 8s 时各级降级门槛同步下调（L1=2s / L2b=15s / L2a=3s），
  否则 L1 在 hook 路径永不可达；hook 路径默认关闭关键帧识别（`enable_frame_vision_in_hook=false`）。
- **b23 裸短码修复**：hook 中 `b23.tv/xxxx` 与裸 `b23:xxxx` 均可正确构造目标。
- **异常信息脱敏**：解析失败不回显重定向 URL 等细节进群聊（防半盲 SSRF 探测），详情仅进日志。
- **Tool 路径补外层 wait_for**：与命令路径一致的双层超时。

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
| `trigger.hook_total_timeout_sec` | 8 | 自动检测链总预算（v1.0.1 由 25 下调，避免阻塞消息主流程） |
| `trigger.command_total_timeout_sec` | 150 | /bili 命令链总预算 |
| `trigger.enable_frame_vision_in_hook` | false | hook 路径是否启用 L2b 关键帧识别（默认关闭，命令/Tool 路径不受限） |

## 权限/能力

- capabilities：`send.text`（命令回复）、`llm.generate`（L2 关键帧识别与总结）、`api.call`（小程序卡片 NapCat get_msg 回查，v1.0.5 起声明）
- 不调用适配器 HTTP API，不需要 NapCat 开 HTTP 服务器；get_msg 走插件 API 隧道
  （Host 转发到 Napcat-Adapter 插件暴露的 `adapter.napcat.message.get_msg`）
- Napcat-Adapter 需为官方 Mai-with-u 版 v1.2.0+（该版本起暴露插件 API）

## v1.0.5 修复：api.call 能力声明 + 反查观测

- **根因（18:53 真机日志）**：`get_msg(ctx.api) 失败: RPCError`——manifest 未声明
  `api.call` 能力，Host 拒绝转发。对比上游 v4.1.0 manifest 确认其声明了 `api.call`。
- manifest 补 `api.call` 能力声明。
- get_msg 失败日志带 RPCError 详情（错误码/原因，截断 200 字符）。
- 标题反查失败原因透出：`last_search_error` 区分「接口异常（code 412 风控等）」与
  「接口正常但无结果（冷门/下架）」。
- 搜索接口加随机 buvid3 cookie：search 类接口无此 cookie 常被 -412 风控。

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
