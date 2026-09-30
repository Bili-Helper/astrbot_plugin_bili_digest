# Bilibili 省流助手

AstrBot 视频总结插件。默认在群聊发送 **@机器人 来个省流 视频链接**，或回复视频分享卡片后发送 **@机器人 来个省流**。私聊默认不需要 @。

支持文本链接、B 站短链接、JSON 小程序卡片、XML 分享内容和 Reply 消息链。当前消息链接优先；一条消息处理第一个链接。B 站分 P 保留 p 参数。

## 安装

推荐 AstrBot 4.28.1 或更新版本。代码按 4.28.1 源码核对，最低版本声明为 4.16.0，旧版本尚未实机验证。

1. 在 AstrBot 插件管理页通过仓库链接安装：`https://github.com/Bili-Helper/astrbot_plugin_bili_digest`。也可将此文件夹放到 AstrBot 的 data/plugins/astrbot_plugin_bili_digest/，确保 main.py 在该目录第一层。支持本地上传的插件管理页也可上传发布 ZIP。
2. 在 **运行 AstrBot 的 Python 环境**安装依赖：

   ~~~bash
   python -m pip install -r data/plugins/astrbot_plugin_bili_digest/requirements.txt
   ~~~

3. 配置 AstrBot 聊天模型。provider_id 留空时，跟随当前会话的模型。
4. 默认方案为 subtitle → asr。无字幕视频需要 ffmpeg、ffprobe，以及 AstrBot STT 服务或本地 faster-whisper。
5. 重启或重新加载插件。修改配置后重新加载以生效。

Docker 部署时，依赖、ffmpeg、Cookie 和模型文件须在 **容器内**可见。工作区的 .reference/AstrBot 仅作开发参考，不需要复制。

## 用法

~~~text
@机器人 来个省流 https://www.bilibili.com/video/BV1xx411c7us
@机器人 来个省流 https://www.bilibili.com/video/BV1xx411c7us?p=2
[回复视频卡片] @机器人 来个省流

@机器人 省流记录
@机器人 省流记录 Python
@机器人 省流查看 记录ID的前8位
@机器人 省流帮助
~~~

BV12345 只是占位符，不是有效 BV 号。

其他适配器需要提供可读取的 Reply.chain 或分享内容。OneBot v11（例如 NapCat）支持通过 get_msg 补取原消息。卡片截图无法直接提取链接；适配器只提供图片时需补发 URL。

## 触发配置

| 需求 | 设置 |
| --- | --- |
| 默认 @ + 来个省流 | trigger_mode=keyword、require_mention=true |
| 修改口令 | keywords=["来个省流","总结一下"] |
| 直接发送 BV 号 | bare_bv=true、require_mention=false |
| 见到视频链接就处理 | trigger_mode=auto、require_mention=false |
| 正则触发 | trigger_mode=regex，设置 trigger_regex |
| 私聊也要求 @ | private_without_mention=false |

正则示例：^(来个省流|总结一下)。正则只检查当前消息纯文本，不使用回复内容触发；匹配限制 2000 字且有超时。

裸 BV 开关允许 BV 号单独触发，仍受 @ 设置限制。省流记录/省流查看/省流帮助命令名固定。

默认允许 Bilibili、b23、YouTube、抖音、西瓜和 AcFun，可修改 allowed_domains。平台可用性取决于 yt-dlp、登录状态和平台限制；域名列表不代表每个平台都经过实机验证。

## 分析方案

strategies 是有序列表，失败后尝试下一项，**首个成功即返回**。想分析画面，请将 frames 或 gemini 放在首位。

| 名字 | 实际流程 | 需要 |
| --- | --- | --- |
| subtitle | 获取 CC/自动字幕 → 分段归纳 → LLM | 聊天模型，部分字幕需 Cookie |
| asr | 下载音频 → ffmpeg 分段 → STT → LLM | ffmpeg/ffprobe、语音识别、聊天模型 |
| frames | 下载视频 → 均匀抽帧 → 图片模型观察 → 联合字幕/ASR 总结 | ffmpeg/ffprobe、图片模型 |
| gemini | 下载视频 → Google Files API → 原生视频理解 → 尝试删除远端文件 | gemini_api_key、gemini_model |
| bibigpt | 调用 BibiGPT 总结接口，归档返回摘要与可用字幕 | bibigpt_api_key |

### 配置组合

- 低成本起步：["subtitle","asr"]，复用 AstrBot 的 LLM 和 STT。
- 本地转录：asr_backend=faster-whisper。总结也可使用 AstrBot 接入的本地 LLM。
- PPT、操作演示：["frames","subtitle","asr"]，选择支持图片的 vision_provider_id。
- 原生视频理解：["gemini","subtitle","asr"]，填写你账号可用且支持视频输入的模型名。
- 外包流程：["bibigpt"]，不依赖本地下载器或 AstrBot LLM。

本地 Whisper 安装：

~~~bash
python -m pip install faster-whisper
~~~

默认 small / cpu / int8，可改为本地模型目录或 CUDA。首次使用模型名可能下载权重；本地权重就绪后可离线转录。GPU 环境要求见 [faster-whisper 官方说明](https://github.com/SYSTRAN/faster-whisper)。

frames 是抽样观察，可能遗漏短暂画面。gemini 归档模型摘要，不生成逐字转录。第三方方案只在显式加入 strategies 后使用，可能产生服务费用。

### 下载器和 Cookie

- 默认 yt-dlp，元数据和字幕也通过它获取。
- downloader=lux 时，lux_path 指向 Lux 可执行文件。Lux 只替换媒体下载，元数据/字幕仍依赖 yt-dlp。
- yt-dlp 使用 Netscape 格式 Cookie 文件绝对路径。每个任务复制独立 Cookie 文件，避免并发修改原文件。
- Lux 的 Cookie 格式遵循其官方说明，与 Netscape 格式不通用；切换下载器时需更换文件。
- danmaku 是弹幕，不是 CC 字幕；插件不会将其当作视频正文。
- 登录、风控、地区、付费内容和字幕权限可能使解析失败。插件不绕过这些限制。

平台接口变化时，可在 AstrBot 的 Python 环境更新下载器：

~~~bash
python -m pip install -U yt-dlp
~~~

## 归档与整理

默认位置：

~~~text
AstrBot/data/plugin_data/astrbot_plugin_bili_digest/
├── archive.sqlite3        # 权威索引与完整记录
├── records/
│   └── <请求ID>.json      # 每次请求的结构化导出
└── tmp/                   # 正常完成、失败、取消后清理
~~~

每个进入队列的总结请求均有记录，包括缓存命中、失败和取消。字段包含：

- schema_version、请求 ID、创建与结束时间（Unix 秒）。
- 来源会话、发送者 ID、原始 URL、规范化 URL。
- 视频 ID、标题、作者、时长、发布时间、简介、封面、章节等可获得的元数据。
- 按时间组织的 segments、summary、内容来源、方案参数和来源说明。
- 尝试记录、失败原因、缓存来源、模型服务 ID。

不会将 API Key、Cookie 或下载器的带签名媒体直链写入归档。原始链接和简介来自消息/视频本身。SQLite、JSON 包含会话标识及正文，记录长期保留，不自动删除。

聊天查询按 unified_msg_origin 隔离，同群成员可查看本群记录。关键词查询本会话最近 100 条，最多返回 10 条。本地管理员工具可检索和导出所有记录：

~~~bash
# 在插件目录执行，将 DATA_DIR 替换为实际数据目录
python archive_cli.py --data-dir DATA_DIR --query Python
python archive_cli.py --data-dir DATA_DIR --format jsonl --output videos.jsonl
python archive_cli.py --data-dir DATA_DIR --format html --output videos.html
~~~

HTML 是无外部资源的静态页面，可直接打开并用浏览器搜索。导出工具只读数据库。

缓存默认 7 天，按会话、规范化 URL、配置、聊天/STT 服务 ID 隔离。缓存命中沿用内容原始时间，不会因持续访问而无限延期。同一个服务 ID 的内部模型参数或 Cookie 文件内容变化后，可暂设 cache_hours=0 强制重新分析。并发重复请求复用结果，但各自保留记录。

## 资源与失败处理

默认最长 1 小时、单任务临时文件总量 300 MiB、并行 2 个任务、最多 8 个排队/运行任务、总超时 20 分钟，均可调整。

本地流程检查元数据时长、媒体实际时长和临时文件大小。文件大小为周期检查，可能短暂超出阈值，不是系统级磁盘配额。BibiGPT 的视频时长和下载由远端控制。

下载器、ffmpeg、本地 ASR 运行于可终止的子进程。Windows 终止进程树，Unix 终止进程组。长文本会逐段归纳；超过总文本上限时报错，不静默丢弃尾部。插件停用会取消任务。系统强制结束后，临时文件可能残留；再次加载时，未完成记录标记为 interrupted。

## 调研依据

- [AstrBot 调用 AI 文档](https://docs.astrbot.app/dev/star/guides/ai.html)：采用统一 LLM 接口；回复消息和 STT 接口另与源码核对。
- [yt-dlp Bilibili 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)：复用现成视频、分 P 和字幕解析。
- [Lux 官方仓库](https://github.com/iawia002/lux)：作为媒体下载备选。
- [Gemini 视频理解](https://ai.google.dev/gemini-api/docs/video-understanding)：使用上传文件进行多模态理解。
- [BibiGPT 作者维护的 API 说明](https://github.com/JimmyLv/bibigpt-skill/blob/main/skills/bibi/references/api.md)：实现其中的总结接口。
- [Bilibili 网页总结接口的社区记录](https://github.com/melon-444/bilibili-API-collect-fork/blob/master/docs/video/summary.md)：未将该网页内部接口作为本插件依赖，避免把尚未稳定验证的接口列为可用选项。

未集成独立 OCR。硬字幕视频可尝试 frames/Gemini；稀疏抽帧不保证覆盖全部硬字幕。

## 开发与验证

核对源码：AstrBot 4.28.1，commit 6d50e1e3a69a87d6c10704684c4438d5e67ccdfb。

在插件目录的父目录运行：

~~~bash
python -m unittest discover -s astrbot_plugin_bili_digest/tests -v
python -m ruff check astrbot_plugin_bili_digest
python -m ruff format --check astrbot_plugin_bili_digest
~~~

测试覆盖解析、字幕、缓存、会话隔离、崩溃恢复、并发去重、取消、远端接口协议/清理和进程超时。安装 ffmpeg/ffprobe 时会用本地样例验证分段与抽帧。

已用真实 B 站公开视频完成元数据/字幕列表联网检查。AstrBot 入口与远端模型接口采用模拟测试；环境未配置 QQ 会话、LLM/STT、Gemini 或 BibiGPT Key，尚未完成这些服务的真实端到端验收。
