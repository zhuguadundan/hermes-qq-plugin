napcat_qq_bridge

作用
- 接收 NapCat 的 OneBot HTTP webhook 消息
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
- 群聊默认只在 `@机器人` 或“回复机器人消息”时触发
- 默认安全策略是 deny-all，必须显式配置 allowlist 或 `--allow-all`
- 按聊天串行处理消息；同一聊天在处理中收到新消息时，会中断当前 Hermes 进程并合并 follow-up，再继续当前 session
- NapCat webhook 丢事件时，桥会按允许名单轮询最近消息历史做兜底补拉
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
- NapCat 已配置 HTTP Client，把事件回调到本桥服务

关键配置
1. NapCat OneBot HTTP Server
   - 默认示例：`127.0.0.1:3000`
   - 桥发送消息给 QQ 时会调用它
   - NapCat 文档的 HTTP API 是路径式调用，例如 `POST /send_private_msg`

2. NapCat HTTP Client webhook
   - 指向桥服务，例如 `http://127.0.0.1:8096/napcat`
   - `messagePostFormat` 建议设为 `array`

3. Token
   - 如果 NapCat 配了 token，桥也必须传 `--onebot-token`

4. 权限
   - 推荐用 `--allow-user` / `--allow-group`
   - `--allow-all` 只是调试开关，不建议长期使用

Docker 部署注意

场景 1：NapCat 用 `--network host`
- OneBot HTTP Server 要真的监听在宿主机可达地址上，例如 `127.0.0.1:3000`
- 如果桥启动自检报连不上，说明 NapCat 的 HTTP Server 没起来，或者地址/端口配错了

场景 2：NapCat 用 bridge 网络
- 必须映射 HTTP API 端口，例如 `-p 3000:3000`
- webhook 需要能从容器访问宿主机
- Linux 常见做法：
  - `--add-host=host.docker.internal:host-gateway`
  - webhook 配成 `http://host.docker.internal:8096/napcat`

推荐启动

```bash
hermes napcat-qq-bridge run \
  --onebot-url http://127.0.0.1:3000 \
  --onebot-token YOUR_TOKEN \
  --listen-host 0.0.0.0 \
  --listen-port 8096 \
  --webhook-path /napcat \
  --allow-user YOUR_QQ_NUMBER \
  --allow-group YOUR_GROUP_ID \
  --hermes-bin hermes \
  --hermes-workdir /home/dawei/.hermes \
  --hermes-toolsets terminal,file,web \
  --skill voice-auto-transcribe \
  --skill image-understand \
  -v
```

环境变量
- `NAPCAT_ONEBOT_URL`
- `NAPCAT_ONEBOT_TOKEN`
- `NAPCAT_QQ_BRIDGE_HOST`
- `NAPCAT_QQ_BRIDGE_PORT`
- `NAPCAT_QQ_BRIDGE_PATH`
- `NAPCAT_QQ_BRIDGE_ALLOW_USERS`
- `NAPCAT_QQ_BRIDGE_ALLOW_GROUPS`
- `NAPCAT_QQ_BRIDGE_ALLOW_ALL`
- `NAPCAT_QQ_BRIDGE_GROUP_CHAT_ALL`
- `NAPCAT_QQ_BRIDGE_TEMP`
- `NAPCAT_QQ_BRIDGE_STATE_DIR`
- `NAPCAT_QQ_BRIDGE_TIMEOUT`
- `NAPCAT_QQ_BRIDGE_CHUNK_SIZE`
- `NAPCAT_QQ_BRIDGE_POLL_INTERVAL`
- `NAPCAT_QQ_BRIDGE_POLL_HISTORY_COUNT`
- `NAPCAT_QQ_BRIDGE_POLL_BACKFILL_SECONDS`
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

3. webhook 是否能打到桥
   - 启动桥后看：
     - `curl http://127.0.0.1:8096/healthz`
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
