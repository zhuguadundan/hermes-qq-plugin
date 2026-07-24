# 架构说明

## 目标

本项目将 NapCat / OneBot 11 作为 Hermes Gateway 的原生个人 QQ 平台，而不是通过 shell 调用 Hermes CLI 的外部 bridge。

```text
QQ 客户端
  -> NapCat
     -> OneBot HTTP API: 登录状态、历史消息、媒体、发送消息
     -> OneBot WebSocket: 实时入站事件
  -> NapCatClient
  -> NapCatQQAdapter
  -> Hermes MessageEvent / SendResult
  -> Gateway 会话、命令、模型、工具、记忆和 delivery
```

## 模块边界

- `hermes_qq/client.py`: 同步 OneBot HTTP RPC 客户端，支持 path endpoint 和 root action endpoint。
- `hermes_qq/adapter.py`: WebSocket 生命周期、事件标准化、访问策略、批处理、媒体处理以及 Hermes adapter 接口。
- `hermes_qq/types.py`: QQ 私聊/群聊目标的数据结构。
- `gateway_platform_shim/qq.py`: 放在 Hermes import path 中的薄 shim，实际代码仍从本包加载。
- `scripts/patch_hermes_core.py`: Hermes 尚无插件注册机制时使用的兼容层。

## 连接生命周期

1. `connect()` 通过 HTTP `get_login_info` 验证 NapCat，并记录机器人 QQ 号和昵称。
2. adapter 启动 WebSocket 监听任务和 HTTP 健康检查任务。
3. WebSocket 使用可配置的 ping interval 和 ping timeout。
4. 流中断时只更新“当前连接已断开”，不会清除“adapter 应继续运行”的生命周期标志。
5. 监听任务等待配置的延迟后重连。
6. 重连成功后执行近期历史消息回补，然后继续处理实时事件。
7. `disconnect()` 才会停止重连，并取消监听、健康检查和待处理批次任务。

把期望生命周期和瞬时连接状态分开，可以避免一次 WebSocket 异常永久终止 QQ adapter。

## 历史消息回补

历史回补用于覆盖 WebSocket 断线窗口：

- 只查询 `dm_policy: allowlist` 的 `allow_from` 目标；
- 只查询 `group_policy: allowlist` 的 `group_allow_from` 目标；
- 每个目标最多读取 `history_backfill_count` 条；
- 忽略早于 `history_backfill_seconds` 窗口的消息；
- 按时间排序后交给与实时事件相同的处理入口；
- 使用事件去重，避免 WebSocket 与历史接口重复投递；
- 单条格式异常不会中止整个回补批次。

开放策略不会自动枚举所有好友或群，因此不会在重连时无边界拉取历史消息。

## 入站消息

OneBot 事件进入 adapter 后依次执行：

1. 事件标准化和自身消息过滤；
2. 私聊、群聊和群成员访问策略检查；
3. 群聊 @ / `group_chat_all` 规则检查；
4. 事件去重；
5. 文本和媒体 segment 解析；
6. 图片与后续文本的短时间批量合并；
7. 构建 Hermes `MessageEvent`；
8. 命令直接处理，普通消息进入 Gateway 会话处理器。

## 出站消息和媒体

adapter 实现 Hermes 的文本、图片、语音、视频和文档发送接口：

- URL 可直接作为 OneBot media segment；
- 本地文件优先通过 NapCat `upload_file_stream` 上传；
- 必要时退回 OneBot 可接受的本地路径或对应文件 API；
- base64 图片缓存为图片扩展名；
- NapCat 转换后的 base64 语音缓存为 `.mp3`，确保下游按音频识别；
- QQ 不支持消息编辑，因此建议关闭流式中间消息和工具进度。

## 安全边界

- OneBot HTTP/WebSocket 最好只监听回环地址或受信任网络。
- 配置 access token，并在 Hermes 中使用相同 token。
- 使用私聊和群聊 allowlist。
- 不要把真实 token、QQ 号、会话数据或 NapCat 设备状态提交到仓库。
- 远程仓库只恢复插件源代码，不能恢复 `~/.hermes` 或 NapCat 的运行数据。

## 为什么不使用旧 bridge

独立 CLI bridge 容易形成两套运行状态：

- WebUI 或 `/model` 切换与 QQ 会话不一致；
- `/new`、`/reasoning`、`/stop` 等命令语义不同；
- 图片和文字可能被拆成独立 turn；
- 媒体发送绕过 Hermes delivery；
- 每轮消息可能被当成新的 CLI 运行。

原生 adapter 只转换平台事件，高层行为由 Hermes 统一管理。
