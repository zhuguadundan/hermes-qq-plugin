# napcat_qq_bridge

NapCat / OneBot 到 Hermes 的 QQ 桥接组件。

如果你只关心“怎么装、怎么接私聊/群聊、怎么配”，优先看仓库根目录 `README.md`。

这里补充组件级要点。

## 默认路径

- 插件安装目录：
  - `~/.hermes/plugins/napcat_qq_bridge`
- 配置目录：
  - `~/.hermes/napcat_qq_bridge/config.json`
- 临时目录：
  - `~/.hermes/napcat_qq_bridge/tmp`
- 状态目录：
  - `~/.hermes/napcat_qq_bridge/state`

## 组件职责

- 连接 NapCat OneBot WebSocket Server 收消息
- 调用 Hermes CLI 处理消息
- 把 Hermes 返回的文本 / `MEDIA:` 文件发回 QQ
- 为每个聊天维护独立 session
- 处理中收到 follow-up 时中断并合并

## 触发规则

私聊：
- 用户 QQ 号必须在 `private_users`

群聊：
- 群号必须在 `group_ids`
- 默认只响应：
  - `@机器人`
  - 回复机器人上一条消息
- 如果 `group_chat_all=true`，群里所有消息都可能触发

## 关键配置

### onebot
- `url`: HTTP API 地址
- `token`: HTTP token
- `ws_url`: WebSocket Server 地址
- `ws_token`: WebSocket token

### bridge
- `receive_mode`: 推荐 `ws`
- `group_chat_all`: 群里是否全量触发
- `poll_interval`: 可选轮询兜底
- `poll_history_count`: 历史补拉条数
- `poll_backfill_seconds`: 历史补拉时间窗口
- `ws_reconnect_delay`: WS 断开后重连间隔

### auth
- `private_users`: 私聊白名单
- `group_ids`: 群号白名单
- `group_users`: 可选群成员白名单

### hermes
- `bin`: `hermes` 可执行程序
- `workdir`: Hermes 工作目录
- `model`: 可选模型
- `provider`: 可选 provider
- `toolsets`: 允许的工具集
- `skills`: 预加载 skills

## NapCat 建议

- HTTP Server 开启
- WebSocket Server 开启
- HTTP Client 建议关闭
- `messagePostFormat: array`

## 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

关注：
- `websocket_connected`
- `websocket_last_error`
- `receive_mode`
- `seen_events`

## 媒体回发规则

Hermes 最终回复里：

```text
MEDIA:/absolute/path/to/file
```

如果音频要按 QQ 语音发送：

```text
[[audio_as_voice]]
MEDIA:/absolute/path/to/audio.ogg
```

## 常见命令

- `/new`
- `/reset`
- `/status`
- `/stop`
- `/help`
