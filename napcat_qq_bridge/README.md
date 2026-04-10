napcat_qq_bridge

作用
- 通过 NapCat 的 OneBot WebSocket Server 接收消息（纯 WS 入站）
- 把 QQ 私聊/群聊消息转给本机 Hermes CLI
- 把 Hermes 的文本、图片、语音、视频、普通文件回发到 QQ
- 维护每个 QQ 聊天独立的 Hermes session，并在重启后继续复用

当前实现
- `bridge.py`: 主逻辑
- `cli.py`: CLI 命令注册
- `__init__.py`: Hermes 插件入口

修复后的行为
- 会把同一个 QQ 私聊或群聊绑定到固定 Hermes session，自动 `--resume`
- 群聊 session 默认按“群 + 发言人”隔离，和 Hermes 原生 gateway 的默认行为一致
- 支持 `/new`、`/reset`、`/status`、`/stop`、`/help`
- 支持群聊和私聊的文本、图片、语音、普通文件收发；群文件上传 notice 也会进入会话
- 群聊默认只在 `@机器人` 或“回复机器人消息”时触发
- 默认安全策略是 deny-all，必须显式配置 allowlist 或 `--allow-all`
- 白名单拆分为：私聊用户白名单、群号白名单、可选群成员白名单
- 支持 `--config-file` JSON 配置；默认路径是 `~/.hermes/napcat_qq_bridge/config.json`
- 按聊天串行处理消息；同一聊天在处理中收到新消息时，会中断当前 Hermes 进程并合并 follow-up，再继续当前 session
- WS 重连后会按允许名单做一次历史补拉兜底，避免断线窗口丢消息
- 语音附件按 NapCat 文档走 `get_record`
- 普通文件优先走 `get_private_file_url` / `get_group_file_url` / `get_file`
- 图片会优先尝试 `get_image` 刷新 URL
- 文件上传按 NapCat Stream API 的分片和完成信号格式发送
- 启动时会先自检 NapCat OneBot HTTP API，不通就直接失败，不再静默假死
- 会过滤 Hermes CLI 的 `Resumed session`、终端 UI 外壳、工具进度噪音和重复段落块，不再把这些脏输出发回 QQ

依赖
- 本机可执行 `hermes`
- Python 依赖 `requests`
- NapCat 已登录 QQ
- NapCat 已开启 OneBot HTTP Server
- NapCat 已开启 OneBot WebSocket Server

关键配置
1. NapCat OneBot HTTP Server
   - 默认示例：`127.0.0.1:3000`
   - 桥发送消息给 QQ 时会调用它
   - NapCat 文档的 HTTP API 是路径式调用，例如 `POST /send_private_msg`

2. NapCat 入站方式（纯 WS）
   - WebSocket Server：
     - 默认示例：`ws://127.0.0.1:3001`
     - 桥作为 WS client 主动连上去收消息
   - HTTP Client webhook：
     - 建议关闭
     - 桥在 `receive_mode=ws` 时会拒绝 `/napcat` webhook 入站，避免双路重复投递

3. Token
   - 如果 NapCat 配了 token，桥也必须传 `--onebot-token`

4. 权限
   - `--allow-user`：私聊用户白名单
   - `--allow-group`：群号白名单
   - `--allow-group-user`：可选，允许指定成员在任意群里触发
   - `--allow-all` 只是调试开关，不建议长期使用

5. 配置文件
   - 默认读取 `~/.hermes/napcat_qq_bridge/config.json`
   - JSON 配置优先级：命令行参数 > 配置文件 > 环境变量 > 内置默认值
   - 适合把群聊/私聊白名单、token、Hermes 参数都收进一个文件

Docker 部署注意

场景 1：NapCat 用 `--network host`
- OneBot HTTP Server 要真的监听在宿主机可达地址上，例如 `127.0.0.1:3000`
- 如果桥启动自检报连不上，说明 NapCat 的 HTTP Server 没起来，或者地址/端口配错了

场景 2：NapCat 用 bridge 网络
- 必须映射 HTTP API 端口，例如 `-p 3000:3000`
- 还要映射 WS 端口，例如 `-p 3001:3001`

推荐启动

1. 先准备配置文件（见同目录 `config.example.json`）

```bash
mkdir -p ~/.hermes/napcat_qq_bridge
cp /path/to/hermes-qq-plugin/napcat_qq_bridge/config.example.json ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json
```

2. 然后直接启动

```bash
hermes napcat-qq-bridge run
```

如果你不想放默认路径，也可以显式指定：

```bash
hermes napcat-qq-bridge run --config-file /path/to/napcat-qq-bridge.json
```

配置文件示例

```json
{
  "onebot": {
    "url": "http://127.0.0.1:3000",
    "token": "YOUR_TOKEN",
    "ws_url": "ws://127.0.0.1:3001",
    "ws_token": "YOUR_TOKEN"
  },
  "bridge": {
    "host": "127.0.0.1",
    "port": 8096,
    "path": "/napcat",
    "receive_mode": "ws",
    "group_chat_all": false,
    "poll_interval": 0
  },
  "auth": {
    "private_users": ["YOUR_QQ_NUMBER"],
    "group_ids": ["YOUR_GROUP_ID"],
    "group_users": []
  },
  "hermes": {
    "bin": "hermes",
    "workdir": "/home/YOUR_USER",
    "toolsets": "terminal,file,web",
    "skills": []
  }
}
```

环境变量
- `NAPCAT_ONEBOT_URL`
- `NAPCAT_ONEBOT_TOKEN`
- `NAPCAT_ONEBOT_WS_URL`
- `NAPCAT_ONEBOT_WS_TOKEN`
- `NAPCAT_QQ_BRIDGE_HOST`
- `NAPCAT_QQ_BRIDGE_PORT`
- `NAPCAT_QQ_BRIDGE_RECEIVE_MODE`
- `NAPCAT_QQ_BRIDGE_ALLOW_USERS`
- `NAPCAT_QQ_BRIDGE_ALLOW_GROUPS`
- `NAPCAT_QQ_BRIDGE_ALLOW_GROUP_USERS`
- `NAPCAT_QQ_BRIDGE_CONFIG`
- `NAPCAT_QQ_BRIDGE_ALLOW_ALL`
- `NAPCAT_QQ_BRIDGE_GROUP_CHAT_ALL`
- `NAPCAT_QQ_BRIDGE_TEMP`
- `NAPCAT_QQ_BRIDGE_STATE_DIR`
- `NAPCAT_QQ_BRIDGE_TIMEOUT`
- `NAPCAT_QQ_BRIDGE_CHUNK_SIZE`
- `NAPCAT_QQ_BRIDGE_POLL_INTERVAL`
- `NAPCAT_QQ_BRIDGE_POLL_HISTORY_COUNT`
- `NAPCAT_QQ_BRIDGE_POLL_BACKFILL_SECONDS`
- `NAPCAT_QQ_BRIDGE_WS_RECONNECT_DELAY`
- `HERMES_BIN`
- `HERMES_WORKDIR`
- `HERMES_MODEL`
- `HERMES_PROVIDER`
- `HERMES_TOOLSETS`
- `NAPCAT_QQ_BRIDGE_SKILLS`

聊天命令
- `/new` 或 `/reset`
  - 清空当前聊天绑定的 Hermes session
  - 停掉当前处理中的 Hermes 子进程
  - 清掉这个聊天还没处理的排队消息

- `/status`
  - 查看当前聊天是否在处理中、排队数、当前 session_id

- `/stop`
  - 只停止当前处理，不删除 session

- `/help`
  - 返回桥支持的命令说明

群聊触发规则
- 默认只响应两种情况：
  - 消息里 `@机器人`
  - 回复的是机器人上一条消息

- 想让机器人在群里看见所有消息都回复：
  - 启动时加 `--group-chat-all`

Hermes 回复媒体的规则
- 文本：直接回 QQ 文本
- 图片/语音/视频/文件：在最终回复中用 `MEDIA:/绝对路径`
- 如果音频要按 QQ 语音发送，而不是普通文件：
  - 在回复里额外带上 `[[audio_as_voice]]`

状态文件
- 临时下载目录默认：
  - `~/.hermes/napcat_qq_bridge/tmp`

- 会话状态目录默认：
  - `~/.hermes/napcat_qq_bridge/state`

- session 存储文件：
  - `~/.hermes/napcat_qq_bridge/state/sessions.json`

健康检查
- `GET /healthz`
- 返回桥当前配置摘要、NapCat 传输模式缓存、bot self_id、是否已配置权限

排错
1. 桥能否启动
   - 如果启动时直接报 `startup check failed`
   - 先查 NapCat HTTP Server 是否真的可达

2. NapCat HTTP API 是否通
   - 路径式调用：

```bash
curl -X POST \
  http://127.0.0.1:3000/get_login_info \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

3. WebSocket 是否连上桥
   - 启动桥后看：
     - `curl http://127.0.0.1:8096/healthz`
   - 如果是 WS 模式，`healthz` 里应看到：
     - `receive_mode=ws`
     - `websocket_connected=true`
     - `webhook_ingress_enabled=false`
   - 再给 QQ 发一条消息，看桥端日志是否有入队或处理记录

4. NapCat 是否已登录 QQ
   - 如果容器日志不断刷二维码和登录错误，桥代码没法替你回复
   - 先把 NapCat 登录状态稳定下来

5. 群里没回复
   - 先确认有没有 `@机器人` 或回复机器人消息
   - 或临时加 `--group-chat-all` 排查

6. 发文件/发语音失败
   - 确认 NapCat 版本支持 `upload_file_stream`
   - 确认桥机上的文件路径真实存在
   - 确认 NapCat HTTP API 可用且 token 正确
