# napcat_qq_bridge（legacy）

NapCat / OneBot 到 Hermes 的 QQ 桥接组件。

> 当前状态：**legacy / archive**
>
> 新部署请优先使用 Hermes 主仓库里的原生 QQ 适配器：`gateway/platforms/qq.py`

如果你只关心“现在应该怎么接 QQ”，优先看仓库根目录 `README.md`。

这个文件仅保留组件级补充说明，供历史实现排查和回滚参考。

## 1. 默认路径

- 插件安装目录：
  - `~/.hermes/plugins/napcat_qq_bridge`
- 配置目录：
  - `~/.hermes/napcat_qq_bridge/config.json`
- 临时目录：
  - `~/.hermes/napcat_qq_bridge/tmp`
- 状态目录：
  - `~/.hermes/napcat_qq_bridge/state`

## 2. 组件职责

- 连接 NapCat OneBot WebSocket Server 收消息
- 调用 Hermes CLI 处理消息
- 把 Hermes 返回的文本 / `MEDIA:` 文件发回 QQ
- 为每个聊天维护独立 session
- 处理中收到 follow-up 时中断并合并
- 回复优先取 Hermes 的结构化最终结果，不再依赖 CLI stdout 抓正文

## 3. 当前行为（旧桥实现）

- 同一个 QQ 私聊或群聊会绑定固定 Hermes session，自动 `--resume`
- 群聊 session 默认按“群 + 发言人”隔离
- 支持 `/new`、`/reset`、`/status`、`/stop`、`/help`
- 支持私聊/群聊文本、图片、语音、视频、普通文件
- 群文件上传 notice 会进入会话
- 群聊默认只在 `@机器人` 或回复机器人消息时触发
- 默认安全策略是 deny-all，必须显式配置 allowlist 或 `--allow-all`
- 按聊天串行处理；处理中收到新消息时会中断并合并 follow-up
- WS 重连后会按允许名单做一次历史补拉兜底
- 普通文件优先走 `get_private_file_url` / `get_group_file_url` / `get_file`
- 图片优先尝试 `get_image` 刷新 URL
- 语音优先走 `get_record`
- 文件上传走 NapCat Stream API 分片格式

> 说明：Hermes 原生 QQ 适配器已经支持把 **同一个 QQ 群** 配成共享上下文（`group_sessions_per_user: false`），因此主线方案不再受这个“群 + 发言人隔离”的旧限制。

## 4. 关键配置

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
- `request_timeout`: HTTP 请求超时
- `chunk_size`: Stream API 上传分片大小

### auth
- `private_users`: 私聊白名单
- `group_ids`: 群号白名单
- `group_users`: 可选群成员白名单

### hermes
- `bin`: `hermes` 可执行程序
- `workdir`: Hermes 工作目录
- `model`: 可选模型
- `provider`: 可选 provider
- `toolsets`: 预加载工具集
- `skills`: 预加载 skills

## 5. 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

重点关注：
- `websocket_connected`
- `websocket_last_error`
- `receive_mode`
- `seen_events`
- `bot_user_id`
- `bot_name`

## 6. Hermes 媒体回发规则

Hermes 最终回复中：

```text
MEDIA:/absolute/path/to/file
```

如果音频要按 QQ 语音发送：

```text
[[audio_as_voice]]
MEDIA:/absolute/path/to/audio.ogg
```

## 7. 命令

支持：
- `/new`
- `/reset`
- `/status`
- `/stop`
- `/help`
