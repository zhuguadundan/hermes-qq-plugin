# Hermes QQ Plugin

一个可用的 Hermes Agent QQ 插件，基于 NapCat OneBot HTTP，把 QQ 私聊/群聊消息转给本机 Hermes CLI，再把 Hermes 的文本、图片、语音、视频、普通文件回发到 QQ。

这个仓库不是官方 Hermes 仓库，而是从实际修复和联调中整理出来的独立交付仓库，目标是：

- 别人拿到仓库就能复现
- 别人能看懂这个插件为什么这样设计
- NapCat webhook 偶发丢消息时，插件仍然能工作

## 当前状态

这份插件已经完成过真实环境联调，当前能力包括：

- QQ 私聊消息转 Hermes
- QQ 群聊消息转 Hermes
- 每个聊天绑定独立 Hermes session，自动 `--resume`
- 同一聊天串行处理，并支持 follow-up 中断/合并
- `/new`、`/reset`、`/status`、`/stop`、`/help`
- 图片、语音、视频、普通文件收发
- NapCat path-style HTTP API 优先，root-style 兜底
- webhook 正常时走 webhook
- webhook 丢消息时走最近历史轮询兜底

## Review 结论

这次 review 的核心结论只有两条：

1. 最初“QQ 发消息 Hermes 不回”的根因不只一个。
   一部分是插件自身没有对齐 Hermes 原生 messaging gateway 的会话中断模型，只做了普通 FIFO 排队；另一部分是 NapCat HTTP Client 对桥接 webhook 的上报在现场环境里有丢事件现象。

2. 只修桥接会话逻辑还不够。
   如果 webhook 丢消息，桥根本收不到事件，所以最终交付版本在保留 webhook 的同时，又增加了历史消息轮询兜底。

### 已修复的问题

- 按聊天恢复 Hermes session，支持 `--resume`
- 命令类消息不再混入普通对话
- 同一聊天正在运行时，新消息会中断当前进程并合并 follow-up
- Hermes CLI 的 `Resumed session`、终端 UI 外壳、工具进度噪音、重复段落块不再回发到 QQ
- NapCat 发送和上传逻辑对齐 4.17.55 API
- 图片、语音、文件的下载/解析路径修正
- webhook 丢事件时，通过 `get_friend_msg_history` / `get_group_msg_history` 补拉
- 上传文件改为流式分块，不再整文件读入内存

### “重复回复”问题的真实根因

这次现场里看到的“重复回复”有两种表象，但根因不同：

1. webhook 丢事件
   这个会造成“你明明发了 QQ 消息，但 Hermes 根本没回”。

2. Hermes CLI 输出带了终端进度和重复段落
   这个会造成“QQ 收到了回复，但同一条消息内部有一整段内容重复两次”。

第二类问题不是桥把同一条 QQ 消息处理了两次，而是 Hermes CLI 的输出文本本身就重复了段落块，桥以前又把终端 UI 和工具进度原样发回 QQ，所以最终看起来像“重复回复”。

当前版本已经在桥接层做了两层处理：

- 过滤 `Resumed session`、box drawing UI、tool progress 行
- 折叠连续重复的段落块

### 仍然要说明的运行风险

- 历史轮询是兜底，不应替代稳定的 webhook。NapCat 端仍应正确配置 HTTP Client。
- 如果你对非常大的好友列表或群列表使用 `--allow-all`，轮询会增加 NapCat API 压力。生产环境推荐显式 allowlist。
- 群聊冷启动历史补拉是 best-effort。你应该在目标群里做一次实际验证，而不是只验证私聊。

## 插件如何对齐 Hermes 原生消息渠道

Hermes 原生 gateway 的关键机制不是“来一条消息跑一个 CLI”，而是：

- 按 session key 维护活跃会话
- 同一会话有活跃 agent 时，新消息进入 pending
- 非照片 follow-up 会触发 interrupt
- `/stop`、`/new`、`/reset`、`/status` 这类命令走旁路
- 当前轮结束后，runner 会把 pending 消息继续喂回同一个 session

本插件现在刻意对齐了这套模型。

你可以在源码里对照这些位置看：

- `napcat_qq_bridge/bridge.py`
- Hermes 原生 `gateway/platforms/base.py`
- Hermes 原生 `gateway/run.py`
- Hermes 原生 `gateway/session.py`

## 仓库结构

```text
.
├── README.md
├── LICENSE
├── .gitignore
├── napcat_qq_bridge
│   ├── __init__.py
│   ├── bridge.py
│   ├── cli.py
│   └── plugin.yaml
├── tests
│   └── test_napcat_qq_bridge.py
├── scripts
│   └── install-plugin.sh
└── examples
    ├── docker-compose.napcat.yml
    └── systemd
        └── hermes-napcat-qq-bridge.service
```

## 环境要求

- Linux 或 WSL2
- Docker
- 一个已经能登录的 NapCat QQ 容器
- 一个能正常运行的 Hermes Agent
- Hermes 已配置好可用模型

## 第 1 步：安装 Hermes Agent

推荐直接使用 Hermes 官方安装脚本。

### 方案 A：官方一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc
hermes
```

首次进入后，至少完成：

```bash
hermes setup
hermes model
```

你需要确保 Hermes 在本机已经能正常对话，再接 QQ。

### 方案 B：源码安装 Hermes

如果你想从源码安装：

```bash
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
./setup-hermes.sh
source ~/.bashrc
hermes setup
hermes model
```

如果你用源码安装，后面桥接启动时的 `--hermes-workdir` 一般建议指向你的工作目录，而不是插件目录本身。

## 第 2 步：把插件安装到 Hermes

先克隆本仓库：

```bash
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
```

然后执行安装脚本：

```bash
bash scripts/install-plugin.sh
```

它会把 `napcat_qq_bridge` 复制到：

```bash
~/.hermes/plugins/napcat_qq_bridge
```

安装后可验证命令是否注册成功：

```bash
hermes napcat-qq-bridge --help
```

## 第 3 步：启动 NapCat 容器

这个插件默认按 OneBot11 HTTP Server + HTTP Client 的组合来工作。

### 推荐方案：host 网络

这是现场最稳定、最省事的方式。

```bash
docker run -d \
  --name napcat \
  --restart always \
  --network host \
  -e TZ=Asia/Shanghai \
  -e WEBUI_TOKEN=YOUR_WEBUI_TOKEN \
  -v napcat-qq-data:/app/.config/QQ \
  -v napcat-config:/app/napcat/config \
  -v $HOME/.config/napcat:/app/.config/napcat \
  mlikiowa/napcat-docker:latest
```

### docker compose 示例

仓库里已经带了示例：

```bash
docker compose -f examples/docker-compose.napcat.yml up -d
```

### 容器启动后要做的事

1. 打开 NapCat WebUI

```text
http://127.0.0.1:6099
```

2. 用 `WEBUI_TOKEN` 登录
3. 登录你的 QQ 账号
4. 打开 OneBot11 网络配置

## 第 4 步：配置 NapCat OneBot11

推荐配置如下。

### HTTP Server

- Enable: `true`
- Host: `127.0.0.1`
- Port: `3000`
- Access Token: `YOUR_ONEBOT_TOKEN`

### HTTP Client

- Enable: `true`
- URL: `http://127.0.0.1:8096/napcat`
- Access Token: 可留空，或按你的部署方案单独加
- `messagePostFormat`: `array`

### WebSocket

本插件不依赖 WebSocket，可以不开。

## 第 5 步：启动桥接插件

最小可用启动命令：

```bash
hermes napcat-qq-bridge run \
  --onebot-url http://127.0.0.1:3000 \
  --onebot-token YOUR_ONEBOT_TOKEN \
  --listen-host 127.0.0.1 \
  --listen-port 8096 \
  --webhook-path /napcat \
  --allow-user 123456789 \
  --allow-group 987654321 \
  --hermes-workdir /path/to/your/workdir \
  --hermes-toolsets terminal,file,web \
  -v
```

如果你暂时只测私聊，可以先只写：

```bash
--allow-user 你的QQ号
```

如果你还要测群聊，建议显式写出目标群：

```bash
--allow-group 目标群号
```

### 常用参数说明

- `--onebot-url`
  NapCat OneBot HTTP Server 地址

- `--onebot-token`
  NapCat OneBot HTTP Server token

- `--listen-host` / `--listen-port`
  本地桥接 webhook 服务监听地址

- `--webhook-path`
  NapCat HTTP Client 上报路径

- `--allow-user`
  允许的私聊 QQ 号，可重复传多次

- `--allow-group`
  允许的群号，可重复传多次

- `--allow-all`
  接受所有会话，不建议长期使用

- `--group-chat-all`
  群聊不要求 `@机器人` 或“回复机器人消息”，会处理所有群消息

- `--hermes-workdir`
  Hermes CLI 工作目录

- `--hermes-toolsets`
  例如 `terminal,file,web`

- `--poll-interval`
  webhook 丢消息时的轮询间隔，默认 `3`

- `--poll-history-count`
  每次拉取的历史条数，默认 `20`

- `--poll-backfill-seconds`
  启动时历史回补窗口，默认 `600`

## 第 6 步：验证链路

### 先检查 NapCat HTTP API

```bash
curl -X POST \
  http://127.0.0.1:3000/get_login_info \
  -H 'Authorization: Bearer YOUR_ONEBOT_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

### 再检查桥接健康状态

```bash
curl http://127.0.0.1:8096/healthz
```

正常应返回类似：

```json
{
  "ok": true,
  "bot_user_id": "...",
  "bot_name": "...",
  "onebot_url": "http://127.0.0.1:3000"
}
```

### QQ 侧最小验证顺序

1. 私聊机器人发 `/status`
2. 私聊机器人发 `你好`
3. 群里 `@机器人 你好`
4. 再快速连发两条消息，验证 follow-up 中断

### 如果你要直接看 QQ 最近对话历史

```bash
curl -X POST \
  http://127.0.0.1:3000/get_friend_msg_history \
  -H 'Authorization: Bearer YOUR_ONEBOT_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":你的QQ号,"count":20}'
```

这对排查“有没有回复”“回了什么内容”“是不是重复段落”最直接。

## 支持的聊天命令

- `/new`
- `/reset`
- `/status`
- `/stop`
- `/help`

### 语义说明

- `/new` / `/reset`
  清空当前聊天绑定的 Hermes session，并停掉当前处理

- `/status`
  查看当前聊天是否在处理中、是否有排队消息、当前 session_id

- `/stop`
  停掉当前处理，不删除 session

## 群聊处理规则

默认群聊只处理两种情况：

- 消息里 `@机器人`
- 回复的是机器人上一条消息

如果你要让机器人处理所有群消息：

```bash
--group-chat-all
```

## 媒体处理规则

### QQ 发给 Hermes

- 图片：优先通过 `get_image` 解析
- 语音：通过 `get_record` 拉取
- 文件：优先 `get_private_file_url` / `get_group_file_url` / `get_file`

### Hermes 发回 QQ

Hermes 最终回复里如果包含：

```text
MEDIA:/绝对路径
```

桥会自动识别文件并发送。

如果是音频，并且你想按“QQ 语音”发送而不是普通文件，请让 Hermes 同时输出：

```text
[[audio_as_voice]]
```

## 运行机制说明

### 1. Session 管理

- 私聊 session key: `private:{user_id}`
- 群聊 session key: `group:{group_id}:user:{user_id}`

这意味着：

- 私聊天然独立
- 群聊默认按“群 + 发言人”隔离
- 重启后仍能从保存的 session id 继续

### 2. 中断与合并

如果同一个聊天里当前 Hermes 还在跑，又收到新消息：

- 新消息会进入 pending
- 当前 Hermes 子进程会被中断
- 待处理文本会合并
- 然后继续当前 session

这个行为是刻意对齐 Hermes 原生 gateway 的。

### 3. webhook + poll 双通道

优先级如下：

- 正常情况：NapCat HTTP Client -> webhook -> 插件
- 兜底情况：NapCat webhook 漏事件 -> 插件轮询历史补拉

这正是这份插件和最早“不可用版本”的本质区别。

## 推荐的长期运行方式

### 方案 A：tmux / screen

最简单，适合个人使用。

### 方案 B：systemd

仓库里附带了示例：

```bash
examples/systemd/hermes-napcat-qq-bridge.service
```

你可以把它复制到：

```bash
/etc/systemd/system/hermes-napcat-qq-bridge.service
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-napcat-qq-bridge
```

## 测试

如果你要跑测试：

```bash
python -m pytest tests/test_napcat_qq_bridge.py
```

注意：

- 这些测试是插件单测，不会帮你完成 QQ 实机联调
- 真正的可用性仍要靠 NapCat 登录状态和真实 QQ 消息验证

## 排障

### 1. QQ 发了消息，Hermes 没回

先分三段排查：

1. NapCat 是否收到消息
2. 桥接是否收到 webhook 或轮询补拉
3. Hermes CLI 是否能单独回

可直接同时看三处：

```bash
docker logs -f napcat
```

```bash
tail -f ~/.hermes/napcat_qq_bridge/bridge.log
```

```bash
curl http://127.0.0.1:8096/healthz
```

如果 NapCat 日志里能看到“接收 <- 私聊/群聊”，但桥日志没有入队记录，就优先检查 webhook；如果 webhook 不稳定，确认轮询参数和 allowlist 是否正确。

### 2. 桥启动失败

常见原因：

- `--onebot-url` 错
- NapCat HTTP Server 没启
- token 不匹配
- NapCat 还没登录 QQ

### 3. 健康检查正常，但 QQ 还是不回

重点检查：

- NapCat HTTP Client webhook 地址是否真能访问桥
- `messagePostFormat` 是否为 `array`
- allowlist 是否把目标私聊/群聊放进去

### 4. 群聊不回

先确认：

- 你有没有 `@机器人`
- 你是不是在回复机器人消息
- 或者临时打开 `--group-chat-all`

### 5. 附件发送失败

重点检查：

- NapCat 版本是否支持 `upload_file_stream`
- 本机文件路径是否真实存在
- Hermes 输出的 `MEDIA:/绝对路径` 是否正确

### 6. QQ 里看到内容重复两次

先分清是“同一条消息发了两次”，还是“单条消息内部一段文案重复两次”。

- 如果是同一条消息发了两次，查 webhook 和 poll 是否都命中了同一个 `message_id`
- 如果是单条消息内部一段文案重复两次，优先查 Hermes CLI 原始输出

当前仓库已经对第二类问题做了修复，并附带了回归测试。

## 建议的实际部署参数

如果你是单人自用，建议从这个组合开始：

```bash
hermes napcat-qq-bridge run \
  --onebot-url http://127.0.0.1:3000 \
  --onebot-token YOUR_ONEBOT_TOKEN \
  --listen-host 127.0.0.1 \
  --listen-port 8096 \
  --webhook-path /napcat \
  --allow-user YOUR_QQ \
  --allow-group YOUR_GROUP \
  --hermes-workdir /home/yourname \
  --hermes-toolsets terminal,file,web \
  --poll-interval 3 \
  --poll-history-count 20 \
  --poll-backfill-seconds 600 \
  -v
```

## 来源与对应关系

这份实现主要对照了：

- Hermes Messaging 使用文档
  https://hermes-agent.nousresearch.com/docs/user-guide/messaging/
- Hermes gateway 原生实现
- NapCat 4.17.55 API
  https://napneko.github.io/api/4.17.55

最终目标不是“做一个看起来能跑的 demo”，而是“在真实 QQ 消息环境下稳定把消息送进 Hermes，再把结果发回 QQ”。
