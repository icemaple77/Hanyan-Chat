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
| WebUI | 配置编辑 / 角色编辑 / 记忆查看 / 极简动态（发帖+点赞） | `hanyan/webui.py` |

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

```bash
python webui.py
```

默认监听 `127.0.0.1:5001`，登录用户名/密码是 `config.json` 里的 `webui.username`/
`webui.password`。可以：
- 直接编辑整份 `config.json`（保存时自动和默认值深度合并）
- 新建/编辑/删除角色提示词
- 查看/删除记忆文件（滚动记忆 + 核心记忆）
- 简易动态墙（发帖 + 点赞，每个角色一个独立的动态列表）

如果要在局域网/公网访问，把 `webui.host` 改成 `0.0.0.0` 前**务必**把 `webui.password`
和 `webui.secret_key` 改成强随机值（不要用默认的 `CHANGE_ME`），最好再套一层反向代理
+ HTTPS。

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
