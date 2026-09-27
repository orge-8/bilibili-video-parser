# bilibili-video-parser（B站视频解析）

MaiBot 插件 `org.mai-mai.bilibili-video-parser` v1.0.19。自动识别聊天中的 B 站视频
（BV/av 号、bilibili.com 链接、b23.tv 短链、QQ 小程序分享卡片），解析内容并注入消息
上下文供 bot 理解讨论；另提供 `/bili` 命令与 `parse_bilibili_video` 工具。

参考：[YukiSakiko/content_understanding_plugin](https://github.com/YukiSakiko/content_understanding_plugin)
（hook 注入形态）、[Mettafy/bilibili_video_parser](https://github.com/Mettafy/bilibili_video_parser)
（降级链思想）。

## v1.0.19 安全修复：重定向会让凭据外发 / 白名单失效（上线前全检发现）

用 `httpx.MockTransport` 拦截出站请求做了**行为级复现**（静态阅读发现不了，
因为旧代码"看起来"有白名单、有 `raise`）。两处硬伤：

| # | 现象 | 复现证据（v1.0.18 实测） | 影响 | 级别 |
|---|---|---|---|---|
| 1 | 媒体域/字幕域 302 到外部域时，`SESSDATA` **明文发给了外部域** | 出站记录 `[('i0.hdslb.com', cookie=…SESSDATA=…), ('evil.example.com', cookie=…SESSDATA=…)]`，且下载**成功**返回 138 字节 | 登录凭据泄漏（可被劫持 B 站 CDN/中间人利用） | 高 |
| 2 | `_resolve_b23` 的逐跳白名单**没拦住请求**：`follow_redirects=True` 让 httpx 自行跟随中间跳 | 出站记录 `['b23.tv', '169.254.169.254']` —— 云元数据内网地址**已收到请求**；但函数仍抛同样的 ValueError，返回值看不出异常 | SSRF（半盲探测内网/元数据服务） | 中 |
| 3 | `_napcat_get_msg` / `frame_vision` 把 `str(e)` 原样落日志 | 适配器异常消息内嵌 `cookies=SESSDATA=…` 时，登录态明文写进日志文件 | 凭据泄漏（日志） | 中 |
| 4 | `_extract_frames` / `_frames_to_data_urls` 是同步 PIL 操作，直接在 async 里跑 | ticker 并发计数 **0**（事件循环被完全堵死），日志无任何异常 | 关键帧期间 bot 收不到任何消息 | 低 |

### 修法（每处都配了正/负对照用例）

1. **不再自动跟随重定向**（`_new_client(follow_redirects=False)`），改为调用方**手动逐跳**
   处理并校验白名单 —— 白名单从此覆盖**每一跳**，而不只是初始 URL。
2. **凭据按域写入 cookie jar**（`client.cookies.set(k, v, domain=".bilibili.com")`）：
   跨主机跳转时 httpx 按 jar 重新派生 cookie，非该域拿不到任何凭据。
   *（这是 httpx 的真实语义：无域限定 jar 会匹配任意主机；按请求传 `cookies=` 又已被弃用，
   域限定 jar 同时解决两点。）*
3. **凭据与"能不能取图"解耦**：媒体下载走**独立无 cookie client**，
   不在白名单只意味着"不带凭据"，而不是"拒绝取图"，避免白名单漏域名时把正常功能一起杀掉。
4. **统一脱敏** `redact_secrets()`：所有外部来源字符串（异常、适配器返回）落日志前过一遍，
   只吃值、保留键名（`SESSDATA=<redacted>`，排障信息不丢）。
5. **同步 CPU 工作丢线程池**（`await asyncio.to_thread(...)`）。

### 反向验证（证明用例有牙齿）

把断言拿回到 **git 里的 v1.0.18 原始文件**上跑（比"还原写法"更硬）：

| 断言 | v1.0.18 | v1.0.19 |
|---|---|---|
| 媒体 302 后外部域收到凭据 | **FAIL**（外泄 True，返回 138 字节） | PASS（只请求 `i0.hdslb.com`） |
| b23 302 后越界域未收到请求 | **FAIL**（`169.254.169.254` 已收到） | PASS（只请求 `b23.tv`） |
| 切帧期间事件循环仍可调度 | **FAIL**（ticker = 0） | PASS（ticker = 18 / 18 / 18） |

> 第 2 项的隐蔽之处：旧代码**也抛了同样的 ValueError**，返回值完全正常 ——
> 只有出站请求记录能暴露越界访问。

### 无回归确认

`https://api.bilibili.com` 仍照常收到 `buvid3` + `SESSDATA`；白名单内的跳转
（`b23.tv → www.bilibili.com`、`i0.hdslb.com → i1.hdslb.com`）仍能正常解析/下载。

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
| L2 | 关键帧 VLM 识别 + 宿主总结（官方雪碧图切帧，无需 ffmpeg）／ CC 字幕节选 ／ 简介文本 | 按可用性依次尝试；关键帧需视频 ≥60s 且已配置视觉任务（hook 路径下异步识别，完成后注入后续请求上下文） |
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
  否则 L1 在 hook 路径永不可达；hook 路径的关键帧识别转后台（见 v1.0.12，
  由 `enable_frame_vision_in_hook` 控制，默认开）。
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
| `trigger.enable_frame_vision_in_hook` | true | hook 路径是否后台识别关键帧并把总结**注入后续上下文**（不发消息；命令/Tool 路径不受限） |

## 权限/能力

- capabilities：`send.text`（命令回复）、`llm.generate`（L2 关键帧识别与总结）、`api.call`（小程序卡片 NapCat get_msg 回查，v1.0.5 起声明）
- 不调用适配器 HTTP API，不需要 NapCat 开 HTTP 服务器；get_msg 走插件 API 隧道
  （Host 转发到 Napcat-Adapter 插件暴露的 `adapter.napcat.message.get_msg`）
- Napcat-Adapter 需为官方 Mai-with-u 版 v1.2.0+（该版本起暴露插件 API）

## 本地门禁（4 道）

用 devkit venv 跑前三道；第四道必须用**真机 Host 自带的 Python**。

| # | 命令 | 验的是什么 |
|---|---|---|
| ① | `python -m pytest tests/ -q` | 行为与回归（**143 条**，含安全/卫生审计用例） |
| ② | `python tests/smoke_test.py` | fakehost 端到端：组件注册 + 命令/工具/hook 实际调用 |
| ③ | `python ~/.workbuddy/skills/maibot-plugin-prep/scripts/check_plugin.py`（**cwd = 插件目录，不带参数**） | manifest / 组件 / 依赖 / 命名规范静态检查 |
| ④ | `<host>/python-env/python.exe tests/host_load_probe.py` | **真机宿主加载闸**（见下） |

> ③ 认准上面这个副本。`check_plugin.py` 在本机有多份拷贝，`~/maibot-dev-tools/` 下那份是
> **较早的简版**（输出 `[OK]` 且多 24 条），拿它跑出来的数字和历次门禁记录对不上。

安全与卫生审计用例：

| 文件 | 覆盖 |
|---|---|
| `tests/test_parser.py` | 既有 128 条行为/回归 |
| `tests/test_v1019_egress.py` | 出站安全：凭据不外发、逐跳白名单、日志脱敏、同步 CPU 卸载 |
| `tests/test_v1019_hygiene.py` | 文档一致性、版本对齐、注入块守卫、命令正则 |


### 为什么需要 ④ `host_load_probe.py`

`smoke_test.py` 用的是自建 fakehost，**走不到宿主的两道硬闸**：

1. `ManifestValidator` 按 `host_version` 校验 `host_application.min/max_version`
   —— Host 升级后 `max_version` 过低会直接被拒；
2. `PluginLoader._validate_sdk_plugin_contract()` 要求 `on_load` / `on_unload` /
   `on_config_update` **都被覆写**，且 `get_config_reload_subscriptions()` 可调用。

这两道闸只有在真机 Host 上才跑得出来，所以本地 fakehost 永远是绿的。
`host_load_probe.py` 直接 import 宿主 `src.plugin_runtime.runner.plugin_loader`，
对本插件跑一遍 `discover_candidates()` + `load_candidate()`，再核对
`instance.get_components()` 的组件计数，等于**部署前的真机级预演**。

自动定位 Host 根目录（OneKey 的实例目录名不固定，脚本会兜底扫
`%APPDATA%/MaiBotOneKeyDesktop/*/modules/MaiBot`），也可用位置参数或
`MAIBOT_HOST_ROOT` 指定；找不到时**以退出码 0 跳过**（本地无实例不算失败）。

退出码：`0` 通过/跳过、`2` 未发现插件、`3` 加载失败、`4` 组件数不符。
只加载本插件，不触碰同目录其它插件。

v1.0.19 实测（Host 1.3.0 / SDK 2.7.0 / Python 3.12）：

```
[host] version = 1.3.0
[loaded] org.mai-mai.bilibili-video-parser v1.0.19 type=extension
[loaded] capabilities = ['send.text', 'llm.generate', 'api.call']
[components] {'COMMAND': 1, 'HOOK_HANDLER': 3, 'TOOL': 1}
  [COMMAND] bili_parse  (handler=cmd_bili)
  [HOOK_HANDLER] inject_planner_frame_summary
  [HOOK_HANDLER] inject_replyer_frame_summary
  [HOOK_HANDLER] bili_video_detect  (handler=on_incoming_message)
  [TOOL] parse_bilibili_video
[PASS] 宿主加载门禁通过：manifest 兼容 + SDK 契约 + 组件注册全部符合预期
```

v1.0.19 四道门禁：`pytest 159/159`、`smoke PASS`、`check_plugin PASS37 WARN4 FAIL0`、
`host_load_probe PASS`；新增 31 条安全/卫生用例在**宿主自带 Python（3.12 / SDK 2.7.0 /
httpx 0.28.1）**上同样 `31/31`，无 SDK 版本漂移。

## v1.0.16 修复：逐视频判重（全局 marker 会永久压掉后续所有视频）

- **真机 17:35 复盘**（v1.0.15 首次实跑，视频：洛天依壁纸·蝴蝶 `b23:u77MAgA` /
  `BV1JThU6DE9X`）：v1.0.15 的三个探针**全部到位**，三联对账**逐字通过** ——

  | 对账项 | 真机值 | 结论 |
  |---|---|---|
  | planner 探针 | 17:35:33 `session_id='0123456789abcdef0123456789abcdef'，实例 0x260d5c9fbc0` | hook **执行了** |
  | replyer 探针 | 17:36:21 同上会话键与实例 id | hook **执行了** |
  | 入队日志 | 17:35:36 `会话 0123456789abcdef0123456789abcdef；实例 0x260d5c9fbc0` | 键与实例**逐字一致** |

  三种设计内根因（hook 没执行 / 两侧会话键不同源 / 插件重载清队列）**全被排除**，
  且**没有任何未命中告警**。逐条排查后只剩一个**完全静默**的出口 —— 判重。

### 根因：单一全局 marker 是"一次性开关"

v1.0.12～v1.0.15 用 `_BG_INJECT_MARKER = "[B站关键帧总结"` 这一个**全局**字符串判重：

```python
if _BG_INJECT_MARKER in prompt:  return {}   # ← 裸 return，无日志
if _BG_INJECT_MARKER in _item_texts(items): return {}
```

只要**任何一次**注入成功、该 marker 随上下文留存（宿主把 hook 注入的
`SystemMessageItem` 存进会话上下文即会如此），之后**每一个视频**都会被判成
"已注入过"而永久静默跳过 —— 真机表现就是"第一条之后注入日志再也不出现，
且不报任何错"，与 17:35 的现象完全吻合。

### 本版改动

**① 判重改为逐视频**：注入文本改为 `[B站关键帧总结·{video_id}·{title}]`
（带稳定 video_id，标题会改名/重名，不能当键）；判重只看**本视频自己**的前缀：

```python
payload_text = self._payload_text(prompt, messages, items)
already = [e for e in pending if self._marker_for(e["video_id"]) in payload_text]
```

**② 判重命中不再静默**：内容确实已在模型上下文里 ⇒ 记为**已投递**并打 INFO，
既不改写载荷、也不再"白烧"后续轮次：

```
关键帧总结已在上下文中，跳过重复注入并按已投递处理（planner）: BV1JThU6DE9X
```

**③ `_inject_pending` 内再无静默 `return`**：每条不注入的路径都经
`_diag_no_inject` 或显式 INFO 出声（含空文本 entry 不再被误登记为"已投递"）。

**④ 补掉诊断自身的盲区**：v1.0.15 在 `_purge_pending` 删 key 时**联动**清掉了
`_bg_enqueued`，恰好把"曾入队 → 队列被清空"这条最该告警的路径变成静默。
现在新增 `_bg_served`（正常结束痕迹）与 `_bg_enqueued`（入队痕迹）**配对**判断：
两者都在、时间又对不上，才说明内容真丢了。

### 真机验证：注入链路全绿（19:28–19:38，v1.0.16 首次实跑）

视频：`【猫娘计划】插件开发，你也可以`（`b23:ma6Fd0j` / `BV1L2eS69EnT`，1:35，
L1 官方总结未命中 → 降级 L2 字幕 + 后台关键帧双链路）。本窗口 `19:29:54` 有
`Maisaka 运行时已启动`（即该实例刚起），因此**两条探针的首次触发都落在本窗口内**，
证据链完整：

```
19:30:18 关键帧注入探针：planner hook 首次触发，载荷字段=[built_message_count, hook_name,
         item_schema_version, items, selected_history_count, selection_reason, session_id,
         tool_definitions]，session_id='0123456789abcdef0123456789abcdef'，实例 0x273a5b4fce0
19:33:38 关键帧注入探针：replyer hook 首次触发，载荷字段=[attempt, hook_name, item_schema_version,
         items, max_retries, reply_message_id, reply_reason, reply_tool_args, request_type,
         requested_model_name, retry_count, selected_expression_ids, selected_model_name,
         selected_model_visual, session_id, task_name]，session_id='79588bdc…'，实例 0x273a5b4fce0
19:33:56 官方 AI 总结不可用，降级 L2: BV1L2eS69EnT | data.code=1（1=未识别到语音, -1=不支持AI摘要）
19:33:56 关键帧后台任务已孵化（解析→信息→雪碧图→VLM→入队）: b23:ma6Fd0j
19:34:06 关键帧总结已入待注入队列（会话 0123456789abcdef0123456789abcdef，队列 1 条，
         等下一次模型请求注入；实例 0x273a5b4fce0）: BV1L2eS69EnT
19:34:06 关键帧后台任务完成（耗时 9.6s：解析短链 0.2s/取视频信息 0.1s/取雪碧图 0.2s/
         VLM 识别 9.0s/入队 0.0s）: BV1L2eS69EnT
19:36:29 关键帧总结已注入replyer上下文（items，回传完整 kwargs 16 键）: BV1L2eS69EnT   ← ✅ replyer 命中
19:36:45 关键帧总结已注入planner上下文（items，回传完整 kwargs 8 键）: —               ← ✅ planner 命中（尾部 `—` 见 v1.0.17）
```

**这是 v1.0.12 引入上下文注入以来第一次真正注入成功**，四个结论全部落地：

| 观察到的事实 | 结论 |
|---|---|
| 两条探针都出现，且实例 `0x273a5b4fce0` 与入队日志**逐字一致** | 三联对账（hook 执行✓ / 会话键✓ / 实例✓）**已无需再怀疑** |
| 注入日志出现了 | v1.0.15 那个"三联对账全过却零注入"的死局被解开 ⇒ **判重出口确实是真因** |
| 形态都是 **`items`** | 与 17:20 载荷实测一致：真机只有 items 一条通道在跑 |
| **`回传完整 kwargs 16 键` / `8 键`** | 载荷字段数与探针实测**完全吻合** ⇒ v1.0.14 的「完整替换」契约生效，`attempt`/`task_name`/`selected_model_name` 等一个没丢 |

#### 顺带确认：宿主**不**持久化 hook 注入项

19:36:45 的 planner 是**重新注入**（`已注入planner上下文`），而**不是**命中判重
（`已在上下文中`）。这说明宿主**没有**把 replyer 那次注入的 `SystemMessageItem`
存回会话上下文 —— 否则 planner 的 `items` 里会带着 marker、必然走判重分支。

> 这个结论对整个「hook 上下文注入」模式都重要：**注入是"每请求一次性"的，
> 不会跨请求留存**。因此逐视频判重（v1.0.16）在真机上的实际作用是**防御性**的
> —— 它防的是"插件自己的队列在多轮之间重复投递"，而不是"宿主持久化"。
> 也说明 TTL/通道配额回收**必须可靠**，因为宿主不会替你记住"已经给过了"。

#### 端到端生效证据

19:36:45 planner 拿到关键帧总结 → 19:37:13 `工具=reply` → 19:37:15 回复
**「猫娘计划 听着就好可爱 可惜点进去黑屏哈哈」**，且 planner 的
`reply_reference` 写明「视频内容主要是黑屏，实际内容不多」——
"点进去黑屏"属于**画面层**判断，而该视频的 L2 字幕讲的是插件开发技巧、只字未提黑屏，
故这句极可能来自关键帧总结。⚠ 保留一分谨慎：封面描述里也出现了"封面黑底"，
不能算完全铁证。

#### 为什么 planner 到第二轮才命中（设计内，非缺陷）

planner hook 贴着**轮次开始**触发（19:33:52 开始第 2 轮 → 轮内 planner），而队列
**19:34:06** 才就绪 —— **晚了约 10s**，故第 2 轮的 planner 队列还是空的（静默）。
本轮又因模型 `LongCat-2.5-Preview` **硬超时 180s**（19:33:18 切换模型，整轮 Planner
耗时 **213s**）被拉长，replyer 直到 **19:36:29** 才发出并贴着 hook 命中。
这是**第三次**印证：planner 触发早（距请求 22~50s）、replyer 贴着请求，
**replyer 才是迟到内容的第一有效注入点**；planner 由**下一轮**补上。

### v1.0.17：修掉注入日志尾部的 `—`

上表 19:36:45 那行 `: —` 是**日志缺陷、不是注入失败**。原写法读的是
"注入**之后**队列还剩什么"，而这次投递恰好让 planner + replyer 两通道投齐、
条目被 `_purge_pending` **当场回收** ⇒ 读到空 ⇒ 显示 `—`，读起来像"什么都没注入"。
现改为读**本次实际投递的条目**，并附上队列去向：

```
关键帧总结已注入replyer上下文（items，回传完整 kwargs 16 键）: BV1L2eS69EnT；仍在队列 BV1L2eS69EnT
关键帧总结已注入planner上下文（items，回传完整 kwargs 8 键）: BV1L2eS69EnT；已回收：本会话队列已清空
```

一眼即可判断"这一轮投了什么、下一轮还要不要再投"。

### 仍待验证：同一会话**连续两个**视频

本次窗口里只有 `BV1L2eS69EnT` 一个视频进了待注入队列（另外三条
`BV17Bi3BXERy`/`BV1rd4y1a7JG`/`BV1h9bz66E8m` 都命中 **L1 官方总结**，走
`processed_plain_text` 直通路径，不经过队列）。且本次是「整目录替换 + 完整重启」
一起做的，所以**严格说还不能完全排除**一个替代解释：成功只是**重启清掉了残留状态**，
而非逐视频判重生效。判别的决定性实验仍然是 —— **同一会话连发两个 B 站视频**：

| 第二个视频的表现 | 结论 |
|---|---|
| 照常出现 `关键帧总结已注入…` | ✅ 逐视频判重生效（v1.0.16 修复确认） |
| 又变成零注入 | ❌ 还有别的残留状态在压制，回到三联对账 + 判重出口继续查 |

### 其余观察

- `19:34:01 event_loop_watchdog 事件循环卡顿 迟到=0.83s` —— 与 v1.0.15 记录的
  `frame_vision` 同步 CPU 工作（`_extract_frames` PIL / `_frames_to_data_urls` base64）
  同类现象，量级（0.83s vs 17:20 的 5.23s）不足以定论，仍列为低优先加固项。
- 本窗口 `event_loop_watchdog` 还报了 `19:30:16 loop=main 迟到=6.06s`（+ webui 1.56s），
  但同一时刻叠加了记忆双路检索、向量索引补建、多路模型超时重试等**宿主侧**重活，
  归因到本插件不成立。
- **迟到内容的固有窗口**：若 planner 秒回，replyer 可能在 19:34:0x 就发出
  （早于/紧贴 19:34:06 入队）⇒ 那轮必然 miss，靠下一轮补投。TTL 15 分钟足够宽，
  但要注意"补投"这一点决定了**用户看到画面理解要等下一轮**。
- 环境噪声（与插件无关）：`LongCat-2.5-Preview` 多次 30s 网络超时，
  `19:37:14 repeater-recall 自评 LLM 调用失败（RPCError E_TIMEOUT 30000ms）`，
  `character-recognizer` 视觉请求 60s 超时 + `anime_trace` HTTP 429。

- 门禁：pytest **119/119**（`TestV116PerVideoDedup` 12 条：v1.0.16 新增 9 条 +
  真机补测 2 条（replyer 先命中 / planner 下一轮补投、注入项持久化）+ v1.0.17 新增 1 条
  （注入日志必须报出实际投递项）；另有 2 条 v1.0.12 时代前提过时的用例就地改写）；
  smoke PASS（command1/tool1/hook_handler3）、check_plugin PASS37 WARN4 FAIL0。

## v1.0.18 修复：关键帧失败原因必须报真话（超时 ≠ 「VLM 返回空」）

- **真机 19:51 复盘**（泛中术电台节目 FMInfinity，两个视频 `BV1W9DUBME3W` /
  `BV1ezKf6GEqx`）。这个窗口一次跑出**两条独立结论**：

### 结论一：多视频注入确实可用（v1.0.16 修复确认 + v1.0.17 日志修正生效）

```
19:52:12 视频解析命中 L2 视频帧: BV1W9DUBME3W
19:52:25 关键帧总结已入待注入队列（会话 b23:ma6Fd0j…；实例 0x25131dcef00）: BV1W9DUBME3W
19:54:56 关键帧总结已注入planner上下文（items，回传完整 kwargs 8 键）: BV1W9DUBME3W
```

尾部是**正确的视频 id**（不是 v1.0.17 之前的 `—`），说明日志修正生效；同一会话里
另一条视频也在同一窗口正常走完 L2 → 入队链路 ⇒ **"同一会话连续两个视频都注入"
这个 v1.0.16 待验证项，观测上成立**。

> 旁证：队列实例 id 从 19:33 的 `0x273a5b4fce0` 变成 `0x25131dcef00`
> ⇒ 用户在这两个窗口之间**完整重启过 MaiBot**（这也解释了 19:33 片段里探针"不在场"：
> 探针在更早的轮次已打过，重启后重新计"首次"）。

### 结论二（本版修复）：`BV1ezKf6GEqx` 的**超时**被误报成「VLM 返回空」

```
19:55:13 [WARNING] 关键帧识别失败: [E_TIMEOUT] 请求 cap.call 超时 (85000ms)
19:55:13 [INFO]    关键帧后台识别未产出结果（VLM 返回空，耗时 85.5s）: BV1ezKf6GEqx
```

第二行是**谎报**。第一行已经说清"cap.call 超时 85000ms"，第二行却写成"返回空"。

### 根因：双层超时的**触发顺序**

`frame_vision._call_llm` 是 `wait_for(90s)` 包 `call_capability(..., timeout_ms=85000)`：

| 谁先超时 | 抛出的异常 | 是否 `asyncio.TimeoutError` |
|---|---|---|
| 外层 `wait_for(VISION_TIMEOUT_SEC=90)` | `asyncio.TimeoutError` | ✅ |
| **内层 RPC `VISION_RPC_TIMEOUT_MS=85000`**（真机走的就是这条） | SDK 的 `RPCError`，文本 `[E_TIMEOUT] 请求 cap.call 超时 (85000ms)` | ❌ |

内层 **85s 先于**外层 90s 触发 ⇒ 走的是通用 `except Exception` 分支，被吞成
"识别失败"；调用方拿到 `None` 后面又**一概**说成"VLM 返回空"。后果是把排查方向
从"调大超时 / 换更快模型"带偏到"改提示词 / 换模型"。

### 本版改动

| # | 位置 | 改动 |
|---|---|---|
| ① | `frame_vision._is_timeout(exc)` | 新增：认 `asyncio.TimeoutError`/`TimeoutError`、类名含 `timeout`、以及**文本含** `timeout`/`e_timeout`/`超时`（覆盖 `RPCError` 形态） |
| ② | `FrameVisionManager.last_failure_reason` | 新增字段；`analyze()` 开头复位，**每个失败出口**都落精确原因（无雪碧图/下载失败/切帧异常/未配置/超时/空文本…） |
| ③ | `_describe_frames` / `_summarize` 的 `except Exception` | 用 `_is_timeout(e)` 分流：超时报 `帧描述超时（RPC 85000ms 内未返回）`，其余报 `帧描述异常: {e}` |
| ④ | `plugin._vision_failure_reason()` | 新增：读管理器的 `last_failure_reason`；**`getattr` 兼容降级** —— 测试桩/旧实现无该属性时退回 `未返回内容`，绝不因取原因而抛 |
| ⑤ | 后台任务 + 主解析路径的失败日志 | 由写死「VLM 返回空」改为插入真实 `reason` |

修复后同类超时应当看到：

```
关键帧后台识别未产出结果（帧描述超时（RPC 85000ms 内未返回），耗时 85.5s）: BV1ezKf6GEqx
```

- 门禁：pytest **128/128**（`TestV118VisionFailureReason` 新增 9 条：
  `_is_timeout` 三分支（内层 RPC / 标准超时 / 真异常排除）、管理器超时落 reason、
  真·空文本仍报空（反向锁定）、后台日志读真原因、`getattr` 降级、两条源码级守卫）；
  smoke PASS（command1/tool1/hook_handler3）、check_plugin PASS37 WARN4 FAIL0。
  （4 条 WARN 均为已知可接受项：装饰器名与 def 名不同 ×2、声明但未用 api.call/llm.generate、
  `call_capability` 未映射。）

### 本机实例实测（Host 1.3.0 / SDK 2.7.0 / Python 3.12）

在同一台机器上已装有闲置的 MaiBot 1.3.0 实例（OneKey 管理），用它做了**不依赖 QQ 登录**
的宿主级自测：

| 验证项 | 结果 |
|---|---|
| 整目录部署到 `plugins/bilibili-video-parser/` 后宿主启动 | 日志 `bilibili-video-parser v1.0.18 已加载`，纳入 `已加载=7` |
| 真实 SDK **2.7.0**（开发环境是 2.8.1）上跑 pytest | **128/128 全绿** —— 无 SDK 版本漂移 regression |
| `tests/host_load_probe.py`（真机官方 `PluginLoader`） | **PASS**：manifest 兼容 + SDK 契约 + `{'COMMAND': 1, 'HOOK_HANDLER': 3, 'TOOL': 1}` |
| 同目录其它插件失败 | 3 个（`mettafy.bilibili-video-parser-maisaka` / `napcat-adapter` / `snowluma-adapter`），**均因 manifest 的 Host 上限过低**（1.0.0 / 1.1.99 < 1.3.0）被拒，与本插件无关 |

> 该实例整段启动失败的原因不在本插件：同目录的适配器/第三方插件 manifest
> `host_application.max_version` 低于 Host 1.3.0，Host 侧直接拒绝加载，
> 早期初始化因此崩溃。给实例补装了宿主正式依赖 `prompt-toolkit>=3.0.52` 与 `pytest`。



- **真机 17:20 复盘**（v1.0.14 首次实跑）：这是 v1.0.12 以来**第一次干净的验证窗口** ——
  `关键帧总结已入待注入队列` 在 **17:20:22**，而第 1 轮 planner 到 **17:20:35**
  才开始思考，**队列提前 13s 就绪**（不再是"内容还没跑完"那种说不清的情况）。
  分级耗时也证明 v1.0.14 确实生效：`关键帧后台任务完成（耗时 10.5s：解析短链 0.2s/
  取视频信息 0.1s/取雪碧图 0.2s/VLM 识别 9.9s/入队 0.0s）`。
  **但注入日志一行都没有** —— 这次不是窗口问题，是真 bug。

### 为什么当时定位不了：所有非命中路径都是裸 `return`

| 可能的根因 | 当时的可见性 |
|---|---|
| ① hook 根本没执行（未注册 / 熔断 / 实例错位） | **完全不可见** —— 无探针 |
| ② 入队与注入两侧 `session_id` 不同源 | **完全不可见** —— 无探针 |
| ③ 队列被清空（插件重载走 `on_unload` → `_bg_pending.clear()`） | **完全不可见** —— 无探针 |

### 本版改动：三种根因全部变成日志事实

**① 每通道首次触发打探针**（与 cv_lyric_context 的诊断同构）：

```
关键帧注入探针：planner hook 首次触发，载荷字段=['built_message_count', 'hook_name',
'item_schema_version', 'items', 'selected_history_count', 'selection_reason',
'session_id', 'tool_definitions']，session_id='79588bdc…'，实例 0x2152a4e2120
```

探针打在 `session_id` 校验**之前** —— 否则"hook 没执行"这一种永远排除不掉。

**② 未命中统一走 `_diag_no_inject`**，只在可疑时出声：

| 情形 | 输出 |
|---|---|
| 队列里有本会话的项、本通道已投过 | 静默（正常 retry 链路） |
| 队列里有项、本通道没投过、却取不出文本 | `WARNING`（entry 结构异常） |
| **曾入队、但队列里查不到**（TTL 内） | `WARNING` + 完整会话键 + 队列现有键 + 实例 id → 指向根因 ③ |
| **从未入队，但有别的会话 120s 内刚入过队** | `WARNING` → 指向根因 ②（最隐蔽的一种） |
| 队列本来就空（无视频消息的轮次） | 静默（**防刷屏是第一约束**） |

同一 `(通道, 会话)` **60s 节流**。另外，正常回收（两通道都投过）时会**同步清掉
诊断痕迹** —— 否则下一轮会把已完成使命的项误报成"未命中"（假阳性）。

> ⚠ **v1.0.16 已推翻"联动清理痕迹"这个做法**：它把"曾入队 → 队列被清空"这条
> 最该告警的路径一起抹成了静默（诊断自己制造了新的日志盲区）。现改为
> `_bg_enqueued` / `_bg_served` **两张痕迹表配对**判断，见 <a href="#v1016-修复逐视频判重全局-marker-会永久压掉后续所有视频">v1.0.16</a>。

**③ 入队日志打完整会话键 + 实例 id**（原来只打前 8 字符，导致"后半段不同源"
这种根因永远看不出来）：

```
关键帧总结已入待注入队列（会话 0123456789abcdef0123456789abcdef，队列 1 条，
等下一次模型请求注入；实例 0x2152a4e2120）: BV16se36sEQk
```

**三者对账即可定位**：

| 对账结果 | 结论 |
|---|---|
| 探针压根不出现 | hook **没执行**（根因 ①） |
| 入队**实例 id** ≠ 探针实例 id | 插件**重载**过（根因 ③） |
| 入队**会话键** ≠ 探针 `session_id` | 两侧**不同源**（根因 ②） |
| 三者都一致，仍无注入日志 | ⇒ 出口只剩**判重**（v1.0.16 已定位：全局 marker 永久压制） |

### 顺带确认的两件事

- **真机 hook 载荷里没有 `prompt`/`messages`**（17:20:39 planner 8 字段 /
  17:20:53 replyer 16 字段，**都只有 `items`**）。所以 items 分支是唯一有效注入
  路径，另两条仅是版本兼容兜底 —— 此前"三形态"的说法实践上只有一种在跑。
- 文档的 payload 清单是**简化版**：真机多出 `hook_name`、`request_type`、
  `selected_expression_ids`。这恰好说明「必须回传完整 kwargs」的必要性 ——
  文档没列的字段一旦丢掉，影响不可知。

- 门禁：pytest **107/107**（新增 13 用例）、smoke PASS、check_plugin PASS37 WARN4 FAIL0。

## v1.0.14 修复：注入契约（`modified_kwargs` 必须完整回传）+ 通道配额 + 分级耗时

- **真机 16:33 复盘**（v1.0.13 首次实跑）：埋点全部生效，拿到硬数据 ——
  `关键帧后台任务已孵化` → 63.7s 后 `关键帧总结已入待注入队列` →
  `关键帧后台任务完成（耗时 63.7s）`。**但注入日志没有出现**。

### ⚠ 严重潜伏缺陷：`modified_kwargs` 是「完整替换」，不是增量合并

开发文档两处独立写明（《02 装饰器与 ctx 能力清单》：「`modified_kwargs` 必须
完整替换（非增量合并）」；《03 Hook 系统与消息网关》：「完整替换整个 kwargs」）。
而 v1.0.12/v1.0.13 只回传了 `{"items", "item_schema_version"}`——按契约这会**丢掉
其余全部字段**：

| 通道 | 会丢掉的字段 | 后果 |
|---|---|---|
| planner | `tool_definitions` | 模型看不到任何工具，**reply 都调不出来** |
| planner | `built_message_count` / `selected_history_count` / `selection_reason` | 上下文统计与选材依据失真 |
| replyer | `attempt` / `retry_count` / `max_retries` | 重试逻辑失效 |
| replyer | `task_name` / `requested_model_name` / `selected_model_name` / `selected_model_visual` | 模型选择失效 |
| replyer | `reply_message_id` / `reply_reason` / `reply_tool_args` | 回复归属与后处理丢失 |

**该缺陷一直没爆，纯粹是因为队列始终在 hook 触发之后才填充、从未真正注入过。**
修法是回传**完整 kwargs**（`dict(kwargs)` 打底，只覆盖要改的键）——这在「替换」与
「合并」两种语义下都正确，是唯一安全写法。参考实现 cv_lyric_context 有同样问题。

### 回收判据改为「通道覆盖」，不用全局次数

| 版本 | 判据 | 缺陷 |
|---|---|---|
| v1.0.12/1.0.13 | 全局注入次数上限 = 2 | **没有产出回复的规划轮次会白吃配额** |
| v1.0.14 | planner / replyer **各投递一次**，两者都投过才回收 | — |

真机 16:34 的第二轮 planner 就是「没有产出回复」的轮次（结论「无需调用任何工具」，
根本没有 replyer）。全局计数下这轮吃掉配额，**真正成文的那次 replyer 反而拿不到
画面内容**——等于整条 60s 链路白跑。现在 planner 每轮最多投一次（不刷屏），
但配额一定给 replyer 留着；顺序无关；TTL 仍是兜底。

### 分级耗时

原来只有总耗时，看不出 63.7s 是雪碧图慢还是 VLM 慢，而「要不要调小
`max_frames`／换视觉模型」完全取决于这个分布。现在完成与失败日志都带分级耗时：

```
关键帧后台任务完成（耗时 63.7s：解析短链 0.4s/取视频信息 0.3s/取雪碧图 1.2s/VLM 识别 61.0s/入队 0.0s）: BV...
关键帧后台任务失败（阶段：取雪碧图，耗时 3.1s；已完成 解析短链 0.4s/取视频信息 2.7s）: BV...
```

- 门禁：pytest **94/94**（新增 8 用例）、smoke PASS、check_plugin PASS37 WARN4 FAIL0。

- **实测时序（勿再假设「能赶上当轮」）**：本节数据推翻 v1.0.12 的乐观论断。
  16:33 这轮：消息 16:33:44 到达 → 后台任务 **16:34:50 完成（63.7s）**；
  而第 1 轮 planner 在 **16:34:11** 就已开始（消息到达后 27s）、bot 16:34:38 已回复。
  **结构性追不上第一轮**。第 2 轮 planner 16:34:49 开始，入队 16:34:50 —— 差 1 秒。
  结论：关键帧总结定位为「**下一轮知识**」，靠 replyer 链路（它在成文前 4s 才触发）
  与 TTL 兜底，而不是「当轮必达」。详见上节「已知时序特性」。

## v1.0.13 修复：后台链路可观测性（原来整段是"黑箱"）

- **真机 16:16 现象**：L2 字幕 16:16:06 注入，bot 16:16:50 回复；到 16:17:00
  为止日志里**没有后台关键帧的任何一行**。无法判断任务是否孵化、卡在哪一级、
  还是早就静默返回了——只能靠猜。日志盲区本身就是缺陷。
- **每级留痕**（原来全部是裸 `return`，一行日志都没有）：

  | 级别 | 事件 | 级别 |
  |---|---|---|
  | 孵化 | `关键帧后台任务已孵化` | INFO |
  | 孵化 | 跳过：`parse.enable_frame_vision=off` | INFO |
  | 孵化 | 跳过：`trigger.enable_frame_vision_in_hook=off` | INFO |
  | 孵化 | 跳过：视觉管理器未初始化 | WARN |
  | 孵化 | 跳过：同视频 30 分钟内已识别 | INFO |
  | 任务 | 跳过：时长 < 门槛（带实际/门槛秒数） | INFO |
  | 任务 | 跳过：雪碧图未取到 | INFO |
  | 任务 | 跳过：VLM 返回空 | INFO |
  | 任务 | `关键帧后台任务完成（耗时 X.Xs）` | INFO |
  | 任务 | 失败：**带失败阶段名**（解析短链/取视频信息/取雪碧图/VLM 识别/入队） | WARN |

- **解析后用真实 video_id 统一日志与去重键**：孵化时只有原始短码可记（如实
  记录），解析之后的入队/完成日志统一用 `BV…`，与 L1/L2 的
  `视频解析命中 …` 同键，可直接 grep 同一条视频。
- **修真实缺陷：跨链接形态的重复 VLM**。`cache_key` 含 video_id
  （`b23:xxx#p1` vs `bvxxx#p1`），v1.0.12 只用**原始** target 去重，同一视频
  先以 BV 链接、后以 b23 短链分享时判不出重复 → 白跑一次雪碧图 + VLM。
  现解析后按真实 id 再拦一次（`另一链接形态`）。
- **修真实缺陷：重试白烧注入计数**。v1.0.12 是"先取用队列、后判 marker"，
  同一次请求的 attempt/retry 会在**载荷已含 marker** 时仍消耗一格
  `_BG_MAX_INJECT`；该值恰为 2（= planner + replyer 各一格），于是重试会把
  replyer 那一格挤掉——表现为 **replyer 看不到画面内容**。现改为**先判重再
  取用**，判重命中直接放行、不计数。
- **注入 hook 缺会话 ID 不再静默**：`session_id`/`chat_id` 都不在载荷里时
  整个注入功能会永久失效且毫无征兆，现在每通道提示一次（含载荷字段名）。
- 门禁：pytest **86/86**（新增 14 用例）、smoke PASS、check_plugin PASS37 WARN4 FAIL0。

### 已知时序特性：后台总结是"下一轮"的知识，会与首次回复赛跑

后台任务耗时实测 **63.7s**（16:33 真机，含分级耗时可查各段），而 bot 首次回复可能
在 **44s** 内就发出、第 1 轮 planner 更是在消息到达后 **27s** 就开始了。

- 15:27 那次：后台 12s 完成 → **赶上了**当轮 planner。
- 16:16 那次：44s 时 bot 已回复 → 总结只能落到**下一轮**。
- 16:33 那次：63.7s 完成，两轮 planner 都已开始 → **必然**落到下一轮。

三种都不算失败：`maisaka.replyer.before_model_request` 在成文前约 4s 才触发，
是真正的「迟到兜底」注入点；配合 TTL 15 分钟，内容会在后续请求里补上。
但若该会话在 TTL 内没有下一次模型请求，这条总结就自然过期作废。

> 想要"当轮必达"只能把关键帧识别放回同步路径，代价是消息主流程多阻塞
> 60s+，与 8s 预算的设计目标冲突。当前取舍：**不阻塞，允许迟到一轮**。

## v1.0.12 形态变更：关键帧总结注入上下文（不再发消息）

- **真机 15:27 复盘**：v1.0.11 的 `ctx.send.text` 主路径**真机验证通过**
  （`已通过 Platform IO 将消息发往平台 'qq'`），补发链路已彻底修好。
  但产品的**形态**不合适——在聊天流里多发一条 `[B站关键帧总结·…]` 显得突兀。
- **新形态（用户选定）**：后台识别完成后**只入待注入队列**，在随后的
  `maisaka.planner.before_request` / `maisaka.replyer.before_model_request`
  两个 hook 上把总结并入本次模型请求 —— bot 下一轮思考/成文时就能看到视频
  画面内容，聊天里**不再多出任何消息**。
- **注入实现**：兼容三种载荷形态（同 cv_lyric_context 双链路注入）——
  `prompt`（str，追加末尾）／ `messages`（list，追加 role=system）／
  `items`（Context Item schema v1，追加 `SystemMessageItem`，回传
  `item_schema_version`）。两链路都挂，缺一条 planner 决策就看不到画面内容。
- **幂等与回收**：注入文本带 `[B站关键帧总结` 标记，items/messages 里已含
  同一标记则跳过（同一次请求的 attempt/retry 不会叠加）；同一条总结最多注入
  **2 次**（`_BG_MAX_INJECT`）后立即回收，避免在后续多轮对话里反复出现；
  待注入项 TTL **15 分钟**（`_BG_PENDING_TTL_SEC`）；单会话队列上限 **3 条**
  （超出丢最旧）；`on_unload` 清空。
- **实测时序（真机 15:27 时间线）**：L2 字幕 15:27:51 注入 → 后台总结
  15:28:03 完成 → planner 15:28:09 开始思考，当轮赶上了。但这只是**运气好**：
  16:16 那次 44s 时 bot 已回复，总结只能落到下一轮（见 v1.0.13「已知时序特性」）。
- **移除**：v1.0.8 的补发消息路径（`_send_followup` / `_send_result_ok` /
  适配器参数名试探链）整段删除——不再需要主动发消息。

## v1.0.11 修复：补发送达判定（适配器参数名不兼容 + 误报成功）

- **真机 14:42 现象**：日志同时出现两条**互相矛盾**的行——
  `插件 maibot-team.snowluma-adapter 组件 api_action_send_private_msg 执行异常:
  TypeError: ... got an unexpected keyword argument 'user_id'`
  紧接着 `关键帧后台总结已补发: b23:CG1s8f2`。即**消息实际没发出去，却打了成功日志**。
- **根因两层**：
  1. 旧代码硬编码 `adapter.napcat.message.send_private_msg(user_id=...)`。
     该参数名是 napcat-adapter 的约定，**snowluma-adapter 不认**——适配器间
     私聊目标参数名并不统一（`user_id`/`user_uin`/`qq`…）。
  2. Host 把适配器组件的 TypeError **包装成返回值**（`{success:false,error:...}`）
     而非抛出异常，旧代码 `await ...` 后无条件打"已补发"。
- **修复**：
  - **主路径改为 Host 统一 `send` 能力** `ctx.send.text(text, stream_id=...)`：
    按**会话流 ID** 定位目标，由 Host 分派到当前适配器，**与适配器种类无关**，
    不再猜参数名。stream 取 `message["session_id"]`。
  - 适配器 API 降级为**老 Host 兜底**（无 `send` 能力时启用），且**逐个候选
    参数名试探**：私聊 `user_id` → `user_uin` → `qq` → `target_id`，
    群聊 `group_id` → `group_uin`，首个被接受即停。
  - **返回值校验** `_send_result_ok`：兼容 `bool` / `{sent,message_id}` /
    OneBot `{status:"ok",retcode:0}` / 错误包装 `{success:false,error}` /
    `{result:{...}}` 一层嵌套。**未确认送达时打 warning 而非 info**，
    杜绝"失败也报成功"。
- **顺带加固**：补发正文（源自远程视频画面）加与注入块一致的**防注入声明**，
  并按 900 字封顶，避免 VLM 长输出刷屏。

## v1.0.10 修复：VLM 空文本诊断 + L1 命中跳过关键帧

- **真机 14:10 复盘**：L1 官方总结**首次真机命中**（v1.0.7 修复生效，注入带
  `[AI总结]`+`[章节]`）；v1.0.9 三项修复全部验证通过（单注入、后台链路无 -400）。
- **遗留**：后台关键帧 VLM 返回空文本（`_extract_text` 只认 4 个平铺键，
  1.3.0 响应形态变化则提取不到）。修复：兼容 Host 包装 `{success,result}`、
  OpenAI `choices[0].message.content`、嵌套 dict（result/output/message/
  response/answer/data）与纯字符串；仍为空时打响应原文前 300 字符辅助定位。
- **产品逻辑**：L1 官方总结命中时跳过后台关键帧补发（官方总结已够丰富，
  省 20~56s VLM 调用）；仅 L2/L3 降级时补发。

## v1.0.9 修复：双注入真根因 + 后台任务 -400

- **双注入真根因（13:52 真机）**：1.3.0 卡片链接在 processed 与 raw text 段**各出现一次**，
  `extract_targets_from_text` 的 b23 分支没有去重 → 同一链接提取两次 → 主循环
  第二次缓存命中同块（幂等守卫检查的正文在循环结束后才写入，拦不住）→ 双块一次写入。
  修复：b23 提取加 seen 去重 + 循环内块级 set 去重 + **视频粒度 5 分钟注入窗**
  （`_injected_recently`，防双 hook/消息副本场景）。
- **后台任务 -400**：主循环收集的是 resolve 前的原始 target（`b23:xxx`），后台任务
  直接喂 view 接口 → -400 请求错误。修复：`_bg_frame_vision_task` 先 `resolve_target`。
- **L1 -101 账号未登录排查指引**：接口与签名已正确（v1.0.7），-101 说明 SESSDATA
  无效——检查 config.toml 的 `credential.sessdata` 是否完整复制（含 `%2C` 的完整值，
  无首尾空格/引号）、是否过期（重新从浏览器取）。真机运行
  `python tests/probe_conclusion.py <BV号>` 可直接看 nav 登录态验证。

## v1.0.8 功能：关键帧识别后台跑（不阻塞消息流）

> 当时的落地形态是"后台补发一条消息"；v1.0.12 起已改为"注入后续上下文"，见上。

- **背景**：关键帧 VLM 在宿主上单次 20~56s，hook 的 8s 预算塞不下，自动检测
  一直到不了 L2b。阻塞式加预算会让 bot 回复延迟近一分钟（1.3.0 事件循环
  watchdog 已在报卡顿），不可取。
- **方案（后台补发）**：hook 轻量注入（秒级，L1/字幕/简介）完成后，合格视频
  （`enable_frame_vision` 开 + 时长 ≥ `min_video_duration_sec` + 已配置视觉任务）
  自动孵化后台任务跑关键帧 VLM + 宿主总结，完成后经适配器 API
  （`send_group_msg` / `send_private_msg`）补发一条 `[B站关键帧总结·标题]` 消息。
- 体验：视频分享 → bot 立即带基础信息回复 → 数十秒后追加一条关键帧总结。
- 同视频 30 分钟内只补发一次；`/bili` 与 Tool 路径不变（关键帧同步跑）。
- 配置：`trigger.enable_frame_vision_in_hook` 语义变更为"后台补发开关"，默认开。
  不想要补发消息可改回 false（`/bili` 仍可完整解析）。

## v1.0.7 修复：L1 官方 AI 总结接口（从未成功过的隐藏 bug）

- **根因**：`get_ai_conclusion` 接口路径写错（`/view/conclusion` 少了 `/get` 后缀）且
  未做 WBI 签名——该接口要求 **WBI 签名 + SESSDATA 双必需**（限制游客访问），
  L1 自上线起就静默 404 降级，配不配 SESSDATA 都一样。
- **修复**：路径改 `/x/web-interface/view/conclusion/get`，补 WBI 签名
  （复用 `_ensure_mixin_key`），失败原因写入 `last_conclusion_error` 并透出到
  降级日志（区分 `-403 权限不足` / `data.code=1 未识别到语音` / `-1 不支持`）。
- **新增探针**：`tests/probe_conclusion.py`——真机插件目录下运行
  `python tests/probe_conclusion.py <BV号>`，自动读 config.toml 的 SESSDATA，
  依次测 view → nav 登录态 → conclusion/get，直出失败原因。

## v1.0.6 修复：注入幂等守卫（适配 MaiBot 1.3.0）

- **1.3.0 变化**：适配器把小程序卡片的 b23 链接直接写进文本（`链接: https://b23.tv/xxx`），
  主路径直接命中（get_msg 回查对该卡片类型不再触发）。
- **新问题（真机 13:00 实测）**：1.3.0 对同一条消息触发两次 `after_process` hook，
  message_id 去重拦不住（第二次 id 不同/为空），缓存命中后注入块重复两遍。
- **修复**：注入前做内容级幂等检查——注入块已在消息正文里就跳过追加
  （主路径与兜底路径均生效）。不同视频仍各自正常注入，不误伤。

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
| 关键帧总结没进上下文 | 按顺序查日志：① 有没有 `关键帧后台任务已孵化`（没有 → 看紧随的 `后台关键帧跳过：…` 写明的原因：开关关/视觉管理器缺失/30 分钟冷却）；② 有没有 `关键帧后台任务完成（耗时 …：分级耗时）`；③ 有没有 `关键帧总结已入待注入队列`；④ 有没有 `关键帧总结已注入planner/replyer上下文`。任一步缺失都能直接定位到级；`关键帧后台任务失败（阶段：…）` 会指明卡在哪一级 |
| 注入上下文后回复内容仍与视频无关 | 确认后台任务完成时间晚于 bot 首次回复（v1.0.14「已知时序特性」：实测 63.7s，第 1 轮 planner 消息到达后 27s 就开始了）；内容会在该会话下一次模型请求时补上，replyer 链路是兜底注入点 |
| 队列里明明有总结，注入日志却始终不出现 | v1.0.15 起**三行日志对账**：① `关键帧注入探针：{channel} hook 首次触发…实例 0x…` 有没有出现 —— **没有 = hook 根本没执行**；② 探针的**实例 id** 与 `关键帧总结已入待注入队列（会话 …；实例 0x…）` 是否一致 —— **不一致 = 插件重载过，队列被 `on_unload` 清空**；③ 探针的 `session_id` 与入队日志的**完整会话键**是否逐字一致 —— **不一致 = 入队/注入两侧 session_id 不同源**。此外 `关键帧注入未命中：…` 告警会直接写明属于哪一种，无需再猜 |
| 三行对账**全部一致**却仍不注入（v1.0.15 的死角） | 出口只剩**判重**。v1.0.16 起判重命中会打 `关键帧总结已在上下文中，跳过重复注入并按已投递处理（{channel}）: {video_id}` —— 看到它说明**该视频总结已在上下文里**（正常）；若看到的是**别的** video_id 命中而本视频没进，则是 v1.0.16 之前的全局 marker bug（升级即可修）。注意 v1.0.16 前判重是**完全静默**的，属于唯一查不到原因的出口 |

## 许可证

MIT
