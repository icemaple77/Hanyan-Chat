# Hanyan Chat

把 [KouriChat](https://github.com/iwyxdxl) 这类微信 AI 女友机器人的功能，完整迁移到
[Matrix](https://matrix.org) 平台，全程本地推理：本地 LLM（Ollama + qwen3:8b）、
本地 TTS（SAES/GPT-SoVITS）、本地 STT（whisper-mlx，Apple Silicon）。

代码从原来的单文件 `serena_bot.py`（约 1500 行）重新拆分成一个结构化的 `hanyan/` 包，
每个子模块只负责一件事，方便阅读、测试、替换（比如以后想换 STT 引擎或接入实时语音
通话，只用改对应的一两个文件）。

## 功能一览

| 能力 | 说明 | 对应模块 |
|---|---|---|
| Matrix 收发消息 | 手动 sync 循环，DM-only，自动接受邀请，token 失效自动重新登录 | `hanyan/matrix_client.py` |
| 本地 LLM 对话 | Ollama 原生协议 / OpenAI 兼容协议自动切换 | `hanyan/llm_client.py` |
| 消息模板 | `\`/`$` 断句、`[tickle]`/`[tickle_self]`/`[recall]` 标记（映射到 Matrix reaction/撤回） | `hanyan/messaging.py` |
| 角色系统 | 从 `prompts/*.md` 加载，支持 YAML frontmatter，也兼容纯 Markdown | `hanyan/character.py` |
| 多用户独立角色 | 按 Matrix 用户 ID 分配不同角色（`character.user_map`），聊天里也能 `/切换角色` | `hanyan/config.py` + `hanyan/commands.py` |
| 记忆系统 | 滚动短期记忆 + LLM 自动摘要出的核心长期记忆（按重要度/时间衰减淘汰） | `hanyan/memory.py` |
| 情绪 + 表情包 | 关键词检测情绪，从 `emojis/` 对应文件夹随机发一张 | `hanyan/emotion.py` |
| 链接内容提取 | 消息里带链接时自动抓正文摘要给 LLM 参考（纯 stdlib，无 bs4/lxml） | `hanyan/links.py` |
| 定时提醒 | 短期（<10min）/长期一次性/每日重复三种，到点用 Matrix 消息提醒 | `hanyan/reminders.py` |
| 聊天内命令 | `/重启` `/清除临时记忆` `/总结` `/切换角色` 等 | `hanyan/commands.py` |
| 主动消息 | 用户超时不说话时，bot 结合聊天上下文主动发消息（有安静时段） | `hanyan/bot.py` |
| TTS 语音回复 | 调用本地 TTS 网关，WAV 自动压 OGG（Matrix 语音气泡） | `hanyan/tts_client.py` |
| **STT 语音输入（新）** | 本地 whisper-mlx 转写用户发来的语音消息，转完直接进普通聊天流程 | `hanyan/stt_client.py` |
| WebUI | **网页聊天（与 Matrix 会话同步）**/ 配置编辑 / 角色编辑 / 记忆查看 / 极简动态 | `hanyan/webui.py` |
| **工具调用（新）** | 角色可自主搜网页/读链接/搜图/下载图片和表情包/搜 GitHub/查时间 | `hanyan/tools.py` |
| **自我进化（新）** | 每日自我反思更新成长档案，兴趣随聊天演化，表情包库自动扩充，带检查点回溯 | `hanyan/evolution.py` |
| **双模型路由（新）** | 本地模型为主 + 云端（DeepSeek 等）按用途分配，双向 fallback 省 token | `hanyan/llm_client.py` |
| **任务执行（新）** | 多步任务：自动拆解 → 逐步执行（可用工具）→ 自我验证重试 → 汇报+报告落盘 | `hanyan/agent.py` |

### 和 KouriChat 原版的差异

- 通信层从 `wxauto`（Windows 控制微信客户端）换成 `mautrix`（Matrix SDK）。
- 语音电话提醒（`wx.VoiceCall()`）**没有移植**——Matrix 没有直接对应能力，这块留给下一步的
  实时语音通话方案（见文末「下一步」）。
- WebUI 大幅缩小范围：KouriChat 的论坛系统是一个 ~1600 行的"AI 角色自主发帖 + 用户回复
  触发 AI 回复 + 头像上传 + 线程评论"完整模拟社交系统，这里只做了查看/发帖/点赞的最小子集。

## 项目结构

```
Hanyan-Chat/
├── main.py                 # bot 入口：python main.py
├── webui.py                # WebUI 入口（可选）：python webui.py
├── config.json              # 你的真实配置（不提交 git，见下方"配置"）
├── config.example.json      # 配置模板，复制成 config.json 后按需修改
├── requirements.txt
├── prompts/                 # 角色提示词，一个角色一个 .md 文件
│   └── 角色1.md
├── emojis/                  # 情绪表情包，按情绪分文件夹
│   ├── happy/ sad/ angry/ ...
├── data/                    # 运行时数据（记忆/日志/token/提醒等），gitignored
├── scripts/                 # 启动/运维脚本（launchd 安装、start/stop/status/logs）
└── hanyan/                  # 核心代码包
    ├── config.py             # 配置加载（深度合并默认值、原子写入）
    ├── character.py          # 角色加载与多用户路由
    ├── matrix_client.py      # Matrix 收发 + STT 钩子
    ├── llm_client.py         # LLM 调用（Ollama / OpenAI 兼容）
    ├── tts_client.py         # 文字转语音
    ├── stt_client.py         # 语音转文字（whisper-mlx）
    ├── session.py            # 多用户会话状态
    ├── memory.py              # 短期/长期记忆
    ├── messaging.py           # 消息拆句/动作标记解析
    ├── emotion.py             # 情绪检测 + 表情包
    ├── links.py               # 链接内容提取
    ├── reminders.py           # 定时提醒
    ├── commands.py            # 聊天内命令
    ├── bot.py                 # 顶层编排（HanyanBot 主类）
    └── webui.py                # Flask WebUI
```

## 部署

### 1. 系统依赖

```bash
# Ollama（本地 LLM）
# 参考 https://ollama.com 安装后，拉取一个模型（选型见下面「本地模型选型」）：
ollama pull qwen3.5:9b

# ffmpeg（TTS 输出压缩 + STT 解码非 wav 音频都要用到）
brew install ffmpeg   # macOS

# Python 3.10+（用到了 list[dict] 之类的内建泛型标注）
python3 --version
```

#### 本地模型选型

这个 app 对模型的要求：中文角色扮演对话质量、能相对可靠地输出纯 JSON（提醒解析、
记忆摘要都要求严格 JSON 格式，代码里有正则兜底提取，但模型本身输出越干净越好）、
响应速度过得去。跑在 Apple Silicon 统一内存架构上，模型大小要和内存留出余量给
STT（whisper-mlx）和 TTS 服务同时跑，不能占满。

| 统一内存 | 推荐模型 | Q4 量化后大小 | 备注 |
|---|---|---|---|
| 8-16GB（如 Mac mini M4 16GB） | `qwen3.5:9b` | ~5GB | 甜点选择：质量够用，给 STT/TTS 留够内存余量 |
| 8-16GB 但明显卡顿/换页 | `qwen3.5:4b` | ~2.5GB | 三个服务（LLM+STT+TTS）同时跑内存紧张时降级 |
| 18-24GB | `qwen3.5:14b` | ~9GB | 更细腻的角色扮演和更稳的 JSON 输出 |
| 32GB+ | `qwen3.6:27b` | ~17GB | 目前 Qwen 系列里综合最强的可本地跑档位 |

不建议在 16GB 机器上跑 27B/32B 级别模型——加上 macOS 系统本身占用（~4-5GB）和
STT/TTS，会挤爆统一内存触发大量 swap，体验反而更差。

Qwen3/3.5 这类模型支持"思考模式"，默认可能会在回复里夹带 `<think>...</think>`
推理过程，混进正文既不好看，也会干扰消息拆句和 JSON 解析。代码默认通过
`llm.think: false`（见下方配置字段表）关掉这个模式，不需要额外操作；如果你换成
不支持这个参数的旧模型，Ollama 会直接忽略该字段，不影响兼容性。

### 2. Python 依赖

```bash
cd Hanyan-Chat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` 里的 `mlx-whisper` 只支持 Apple Silicon（M 系列芯片）。如果部署在
非 Apple Silicon 机器上（比如 Linux 服务器），装不了这个包不影响其它功能——把
`config.json` 里 `stt.enabled` 设为 `false` 即可，语音消息会被静默跳过，文字聊天完全
不受影响。

### 3. 配置

```bash
cp config.example.json config.json
```

然后编辑 `config.json`（字段说明见下表），至少要改：
- `matrix.homeserver` / `matrix.user_id` / `matrix.password` — 你的 Matrix 账号
- `llm.base_url` / `llm.model` — 默认指向本地 Ollama 的 `qwen3.5:9b`（选型见上面「本地模型选型」）
- `tts.base_url` — 你的 TTS 网关地址；没有 TTS 服务的话把 `tts.enabled` 设为 `false`
- `stt.enabled` / `stt.model` — 非 Apple Silicon 环境设为 `false`

也可以不改 `config.json`，改用环境变量覆盖密码类字段（这两个字段永远不建议明文提交到仓库）：

```bash
export HANYAN_MATRIX_PASSWORD="你的密码"
export HANYAN_WEBUI_PASSWORD="webui密码"
export HANYAN_WEBUI_SECRET="随便一串随机字符串"
```

#### 配置字段参考

| 字段 | 说明 | 默认值 |
|---|---|---|
| `matrix.homeserver` | Matrix 服务器地址 | — |
| `matrix.user_id` | bot 的 Matrix 账号（`@xxx:server`） | — |
| `matrix.password` | 登录密码（首次登录后会缓存 token 到 `data/access_token.txt`，之后不再需要密码，token 失效会自动重新用密码登录） | — |
| `llm.base_url` | Ollama 地址，或任意 OpenAI 兼容端点 | `http://localhost:11434` |
| `llm.model` | 模型名，选型见上面「本地模型选型」 | `qwen3.5:9b` |
| `llm.api_key` | 填了就走 OpenAI 兼容协议（`/chat/completions`），不填走原生 Ollama 协议（`/api/chat`） | 空 |
| `llm.think` | 是否开启 Qwen3/3.5 等混合推理模型的思考模式。这个 app 是纯聊天场景不需要推理链，且 `<think>` 内容混进正文会破坏消息拆句/JSON 解析，默认关闭。只在原生 Ollama 协议下生效，OpenAI 兼容协议没有这个参数 | `false` |
| `tts.enabled` | 是否合成语音回复 | `true` |
| `tts.provider` | `"saes"`（本地网关）或 `"siliconflow"`（云端 API），见下面「TTS 后端选择」 | `saes` |
| `tts.base_url` / `tts.endpoint` | SAES 网关地址（`provider="saes"` 时用） | `http://127.0.0.1:9100` / `/hanyan/stream` |
| `tts.api_key` / `tts.model` / `tts.voice` / `tts.speed` | SiliconFlow 认证/模型/音色/语速（`provider="siliconflow"` 时用） | — |
| `stt.enabled` | 是否转写用户语音消息 | `true` |
| `stt.model` | HuggingFace 上的 MLX 格式 Whisper 模型，首次用会自动下载 | `mlx-community/whisper-large-v3-turbo` |
| `stt.language` | 转写语言，`"auto"` 交给模型自动检测 | `zh` |
| `proactive.interval_minutes` | 用户闲置多久后主动发消息 | `30` |
| `proactive.quiet_start`/`quiet_end` | 安静时段（不主动发消息） | `23:00` ~ `07:00` |
| `proactive.max_per_day` | 每用户每天主动消息上限（防刷屏兜底；同一房间也不会被多个 session 重复触发） | `24` |
| `memory.max_entries` | 滚动短期记忆保留的对话轮数 | `50` |
| `memory.promote_threshold` | 滚动记忆达到多少条时触发摘要成核心记忆 | `30` |
| `memory.core_memory_max` | 核心记忆条目上限 | `50` |
| `character.default_character` | 默认角色名（要和 `prompts/*.md` 里 frontmatter 的 `name:` 一致，不是文件名） | — |
| `character.user_map` | 按用户分配角色：`{"@friend:server": "角色名"}` | `{}` |
| `link_fetch.enabled` | 是否自动抓取消息里的链接内容 | `true` |
| `commands.enabled` | 是否响应 `/命令` | `true` |
| `webui.enabled` | 仅用于提示，实际是否运行 WebUI 取决于你是否执行了 `python webui.py` | `false` |
| `webui.host`/`webui.port` | WebUI 监听地址，默认只监听本机 | `127.0.0.1:5001` |

> `config.json` 加载时会和内置默认值**深度合并**——升级代码后新增的配置字段会自动补全，
> 不会因为你的旧 `config.json` 缺字段就报错或功能静默失效。

#### TTS 后端选择

**本地网关（SAES/GPT-SoVITS，默认）**——完全本地，不需要 API Key：

```json
"tts": {
  "enabled": true,
  "provider": "saes",
  "base_url": "http://127.0.0.1:9100",
  "endpoint": "/hanyan/stream"
}
```

**SiliconFlow 云端 API**——本地没部署 TTS 网关时的备选，需要一个
[SiliconFlow API Key](https://cloud.siliconflow.cn/account/ak)：

```json
"tts": {
  "enabled": true,
  "provider": "siliconflow",
  "base_url": "https://api.siliconflow.cn/v1",
  "api_key": "sk-你的key",
  "model": "FunAudioLLM/CosyVoice2-0.5B",
  "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
  "speed": 1.0
}
```

两种 provider 最终都会尝试用 ffmpeg 把音频压成 OGG/Opus（Matrix 语音气泡
MSC3245 推荐格式），ffmpeg 不可用时会原样发送未压缩的文件，不会因为转码失败就
整条语音消息丢弃。

### 4. 角色

在 `prompts/` 目录放 `.md` 文件，支持两种格式：

**带 frontmatter**（推荐，可以让文件名和角色名不一致）：
```markdown
---
name: 小月
description: 19岁活泼女生
---
你是小月，一个19岁的活泼女生……
```

**纯 Markdown**（兼容 KouriChat 原版风格，文件名就是角色名）：
```markdown
你是XX，一个……

# 性格
……
```

角色提示词里可以用 `\` 指导模型把回复拆成几句话发送（比如"用反斜线分隔句子，不超过
四句"），也可以让模型输出 `[tickle]`（拍一拍）、`[recall]`（撤回上一条）标记，
`hanyan/messaging.py` 会自动解析并映射成对应的 Matrix 操作。

### 5. 启动

#### macOS 推荐方式：launchd（开机自启 + 崩溃自动拉起）

```bash
bash scripts/install-launchd.sh   # 只需要跑一次，装完立即启动
```

之后日常操作：

```bash
bash scripts/start.sh    # 启动
bash scripts/stop.sh     # 停止（优雅关闭，不会被自动拉起）
bash scripts/status.sh   # 看运行状态
bash scripts/logs.sh     # 实时看日志
```

崩溃（非 0 退出）会被 launchd 自动重启；`data/hanyan.lock` 上的 flock 单实例锁
保证任何情况下同时只有一个 bot 进程（手动 `python main.py` 和 launchd 并存时，
后启动的会直接报错退出，不会出现双进程重复回复）。

#### 手动前台运行（调试用）

```bash
python3 main.py
```

首次启动会用密码登录 Matrix，成功后 token 缓存到 `data/access_token.txt`，之后
重启不需要再输密码。日志同时输出到控制台和 `data/hanyan.log`（10MB×3 自动轮转）。

用 `Ctrl+C` 或发 `SIGTERM` 优雅停止（会等当前提醒线程收尾）。

#### 长期运行（Linux systemd 示例）

```ini
# /etc/systemd/system/hanyan-chat.service
[Unit]
Description=Hanyan Chat
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/Hanyan-Chat
ExecStart=/path/to/Hanyan-Chat/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
Environment=HANYAN_MATRIX_PASSWORD=xxx

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now hanyan-chat
journalctl -u hanyan-chat -f
```

（`/重启` 命令内部用 `os.execv` 原地重启进程；如果那样失败会直接退出，交给 systemd 的
`Restart=on-failure` 接管。）

### 6. WebUI（可选）

**内嵌模式（推荐）**：`config.json` 里 `webui.enabled: true`，WebUI 随 bot 一起启动
（默认 `http://127.0.0.1:5001`）。「聊天」页和 Matrix 是**同一段对话**：共用
session、记忆、成长档案和工具，打开自动加载历史记录，网页上聊的内容她在
Element 里也记得（反之亦然）。聊天身份默认绑定最活跃的 Matrix 用户，可用
`webui.chat_user_id` 固定。注意：网页消息不会出现在 Element 的聊天记录里
（没有走 Matrix 协议），同步的是"她的记忆"而不是消息流。

**独立模式**：

```bash
python webui.py
```

独立进程运行时聊天退化为共用磁盘记忆（上下文依然连续，但不共享内存态 session）。

默认监听 `127.0.0.1:5001`，登录用户名/密码是 `config.json` 里的 `webui.username`/
`webui.password`。功能：整份配置编辑（保存自动深度合并）、角色提示词管理、记忆查看/
删除、模型配置、简易动态墙。对外访问方案见下方「对外发布」。

#### 语音通话（/call）

「通话」页是免提轮流对话：点开始后浏览器持续听麦克风（本地音量 VAD 检测你说完），
自动走 STT → LLM → TTS，她的语音播完自动继续听。轮流制天然避免回声（她说话时不收音）。
通话记忆与文字聊天、Matrix 完全同一份。

- 延迟约 3~6 秒/轮（STT+LLM+TTS 串行），是"对讲机感"而非真电话，后续可做流式优化
- 默认通话中不启用工具（`webui.call_tools: false`）以压延迟
- 手机使用：`webui.host` 改成 `0.0.0.0`，手机浏览器访问 `http://<Mac的局域网IP>:5001/call`。
  注意 iOS Safari 要求 HTTPS 才给麦克风权限（`localhost` 除外），局域网 http 下
  Android Chrome 可在 `chrome://flags` 的 unsafely-treat-insecure-origin-as-secure
  加白名单，或给 WebUI 套一层自签 HTTPS/Tailscale
- Android App 即 WebView 包装此页面（见下方「手机 App」），管线全复用

## 对外发布（域名 + nginx 反代）

想在外网/手机上用（尤其语音通话——WebView 的 getUserMedia **只在 https 下放行**），
给 WebUI 套一层 nginx HTTPS 反代即可。Flask 保持只听本机（`webui.host: 127.0.0.1`），
外面全部由 nginx 接：

```nginx
# /etc/nginx/conf.d/hanyan.conf
server {
    listen 443 ssl http2;
    server_name hanyan.example.com;          # 换成你的域名

    ssl_certificate     /etc/letsencrypt/live/hanyan.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hanyan.example.com/privkey.pem;

    client_max_body_size 20m;                # 通话录音分片上传

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;             # LLM 生成 + TTS 合成可能较久
        proxy_send_timeout 300s;
    }
}

server {
    listen 80;
    server_name hanyan.example.com;
    return 301 https://$host$request_uri;
}
```

配好后 `config.json` 里做三件事：

1. `webui.behind_https: true` — 登录 cookie 加 Secure/HttpOnly 标记
2. `webui.password` 换成强密码、`webui.secret_key` 换成长随机串——这个页面
   能改配置、能读记忆、能批准文件操作，**等于 bot 的管理后台**，暴露公网前必须换
3. `webui.host` 保持 `127.0.0.1`（nginx 和 bot 同机时不要开 0.0.0.0）

可选加固：nginx 层再加一层 `auth_basic`、fail2ban 盯 `/login`、或干脆走
Tailscale/WireGuard 不暴露公网。证书用 certbot：`certbot --nginx -d hanyan.example.com`。

## 手机 App（Android）

`android/` 是一个极简 WebView 壳工程：加载你反代出来的 WebUI 域名，桥接麦克风
权限给 `/call` 通话页，Cookie 持久化保持登录态，通话时屏幕常亮。服务器地址
首次启动时填（右上角菜单可改）。

构建：

```bash
# 方式一：Android Studio 打开 android/ 目录 → Build → Build APK(s)
# 方式二：命令行（需要 Android SDK）
cd android && ./gradlew assembleDebug
# 产物在 android/app/build/outputs/apk/debug/app-debug.apk，直接传手机安装
```

个人自用装 debug 包即可；想要正式签名包再配 signingConfig。
注意：服务器地址必须是 **https** 域名，`http://192.168.x.x` 局域网地址在
WebView 里会被内核拒绝麦克风权限（文字聊天不受影响）。

## 工具调用与自我进化

### 工具调用

`tools.enabled: true` 时，每轮对话会给模型注入工具说明，模型输出
`<tool>{"name":"web_search","args":{"query":"…"}}</tool>` 即触发执行，结果喂回后
继续生成（单轮最多 `tools.max_calls_per_turn` 次，防死循环）。可用工具：
`web_search`、`fetch_url`、`search_images`、`download_image`（可顺带收藏进表情包库）、
`github_search`、`get_time`。当前时间同时每轮注入 system prompt，她随时知道现在几点。

搜索后端默认 DuckDuckGo（免 key）。想要更稳、支持图片搜索、反 robot 检测，自建一个
SearXNG 然后配 `tools.searxng_url`：

```bash
docker run -d --name searxng -p 8080:8080 searxng/searxng
# config.json: "tools": {"searxng_url": "http://127.0.0.1:8080"}
```

### 任务执行（规划 → 执行 → 验证 → 汇报）

聊天里说 `/任务 帮我调研一下本地TTS方案并写份笔记`，或她自己判断请求复杂时调用
`start_task` 工具，就会进入后台任务流程：LLM 先把目标拆成 ≤`agent.max_steps`（默认5）
个步骤并播报计划，然后逐步执行（每步都能用全部工具），做完自我验证——发现哪步没做好
会自动重做一次，最后给出口语化总结，完整报告存进她的工作区
`data/workspace/reports/`。`/任务状态` 看进度，`/停止任务` 中止。

保守约束：全局同时只跑一个任务；工作区外的写/删在任务里同样走审批单（任务不等审批，
单号写进汇报，你 `/批准` 后生效）；全程 `[CKPT:task_*]` 日志可回查。

### 本地文件访问（三层权限 + 审批流）

`fs_list` / `fs_read` / `fs_write` / `fs_delete` 四个工具，权限模型（`hanyan/fs_access.py`）：

1. **她的工作区**（`fs.workspace_dir`，默认 `data/workspace/`）— 自由读写删，是她自己的空间
2. **只读范围**（`fs.read_roots`，默认你的家目录）— 只能读文件和列目录
3. **工作区外的写/删** — 生成审批单，她会在聊天里报编号，你回 `/批准 <编号>` 才执行，
   `/拒绝 <编号>` 作废，默认 15 分钟过期，`/待批` 查看队列

安全机制：路径先 realpath 解析（防 `../` 和符号链接逃逸）；`.ssh`/密钥/token/
Keychains 等敏感路径硬编码黑名单，**任何配置都不能放开**（防角色被话术诱导读私钥）；
单文件读取上限 `fs.max_read_kb`；所有操作记审计日志 `data/fs_audit.log`。

### 自我进化（只动数据，不动代码）

进化全部发生在 `data/growth/<用户>_<角色>/` 下的数据层，**永远不自动修改代码**：

- `profile.md` — 成长档案。每天过了 `evolution.reflect_hour`（默认 4 点）自动做一次
  自我反思：读最近聊天，第一人称重写"关于他 / 我们的相处 / 我的变化"，每轮对话注入
  提示词，性格和了解随时间真实演化。`/反思` 可手动触发。
- `interests.json` — 兴趣清单。反思时提取，旧兴趣按 0.85/天衰减、低于 0.2 淘汰，
  兴趣会自然转移；主动消息会聊"最近在研究的东西"。`/兴趣` 查看。
- `proposals.md` — 功能提案。她用 `github_search` 调研到的项目自动归档，想要的新能力
  写在这里**由人审核决定是否实施**（不自动合并代码——那是安全边界）。`/提案` 查看。
- 表情包库 — `download_image` 带 `emotion` 参数时图片存进 `emojis/<情绪>/`，越用越丰富。

**看门狗与回溯**：所有档案写入都走"备份 → 写临时文件 → 校验 → 原子替换"，校验失败
自动还原 `.bak` 备份；关键路径日志统一带 `[CKPT:*]` 标记（`grep CKPT data/hanyan.log`
即可自查全链路），进化文件出问题可手动从同目录 `.bak` 恢复。

### 双模型路由

`llm.cloud.base_url` 配了云端模型（OpenAI 兼容，DeepSeek/SiliconFlow 均可）后：
`use_for` 里的用途（默认 `tools` 工具决策 + `reflection` 反思摘要，低频但吃理解力）
优先走云端，日常聊天仍走本地省 token；任一侧失败自动切另一侧
（日志 `[CKPT:llm_fallback]`）。不配则纯本地，行为不变。

## Matrix 服务端（Synapse）配置要点

bot 对 Synapse 有两个特殊要求，配置在 `~/infra/matrix/data/homeserver.yaml`
（Docker 部署，`docker compose up -d --force-recreate` 生效，改前先备份）：

**限流必须用现行配置名。** 旧版的 `rc_messages_per_second` / `rc_joins_per_second`
等写法早已废弃，新版 Synapse 会静默忽略 → 实际跑在默认限流（0.2 条/秒）上，bot
分条发消息立刻撞 `M_LIMIT_EXCEEDED (Too Many Requests)`。正确写法是嵌套结构的
`rc_message:`（`per_second` / `burst_count`），以及 `rc_login:`、`rc_joins:`。

**媒体要设自动清理。** bot 每条回复都可能上传 TTS 语音和表情 GIF，`media_store`
会无限增长，用 `media_retention.local_media_lifetime: 30d` 让 30 天前的媒体自动
删除（文字聊天记录不受影响，只是老语音不能再播放）。

单用户私服还建议：`presence.enabled: false`（省 sync 开销）、listener 的
`resources.names` 只留 `client` 去掉 `federation`（不联邦，手机 Element 和 bot
都走 client API 不受影响）、docker-compose 里给容器日志加 `max-size` 限制
（默认 json-file 不限大小）。

## 聊天内命令

在和 bot 的私聊里发送（不区分角色，任何用户都能用）：

| 命令 | 别名 | 作用 |
|---|---|---|
| `/重启` | `/re` | 重启 bot 进程 |
| `/关闭主动消息` | `/da` | 暂停主动找你聊天 |
| `/开启主动消息` | `/ea` | 恢复主动消息 |
| `/清除临时记忆` | `/cl` | 清空这段对话的滚动记忆 |
| `/总结` | `/ms` | 立刻把最近聊天摘要进长期记忆 |
| `/切换角色 角色名` | `/role 角色名` | 只切换你自己看到的角色（写入 `character.user_map`） |
| `/角色列表` | `/roles` | 查看有哪些角色 |
| `/帮助` | `/help` | 显示命令列表 |

## 开发 / 测试

没有 CI，但每个模块基本是无副作用的纯函数或小类，可以直接在解释器里单独验证，例如：

```python
from hanyan import messaging
messaging.split_reply("你好呀\\今天天气不错\\[tickle]")
# [('text', '你好呀'), ('text', '今天天气不错'), ('tickle', '')]
```

`hanyan/matrix_client.py` 依赖真实的 `mautrix` 包才能跑，本地想脱离 Matrix 测试其它
模块（`messaging`/`memory`/`commands`/`links`/`character`）时，可以在 `sys.modules`
里塞几个空壳模块占位，绕开 `import mautrix`——具体做法参考仓库历史里用到的测试脚本
思路（stub `mautrix`/`mautrix.client`/`mautrix.types` 三个模块）。

## 已知限制 / 下一步

- **没有语音电话/实时语音通话**——这是刻意留白的下一步：你提到的
  [KoljaB/LocalAIVoiceChat](https://github.com/KoljaB/LocalAIVoiceChat)（Whisper STT +
  Coqui XTTS + 本地 LLM，全本地流水线，~500ms 延迟）和 Rapida（WebRTC 流式编排）都是
  可行方向。现在这版的 `stt_client.py`（whisper-mlx）和 `tts_client.py` 已经是本地
  STT/TTS 的雏形，下一步做实时语音通话时，大概率是新增一个 WebRTC/低延迟音频通道，
  复用现有的 LLM 对话逻辑（`hanyan/bot.py` 里的 `_on_message` 核心流程），而不是
  重新搭一套——但具体要不要复用 Matrix 的语音通话信令（Matrix 本身有 VoIP/MSC 系列
  提案）还是完全独立于 Matrix 搭一条通道，需要另外评估，这里先不展开。
- WebUI 论坛是最小子集（无 AI 自动发帖、无线程评论、无头像上传）。
- STT 目前仅支持 Apple Silicon（whisper-mlx）；换其它平台需要把 `stt_client.py` 换成
  `faster-whisper` 之类的实现，接口（`transcribe(path) -> Optional[str]`）不用变，
  上层代码不用动。
- `config.json` 里如果沿用了旧项目（serena-bot）的 Matrix 密码，建议尽快在 Matrix 账号
  设置里改掉——那个密码曾经被提交进过本地 git 历史。
