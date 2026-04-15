# Hermes QQ Plugin

一个把 NapCat（个人 QQ / NTQQ）接到 Hermes 的 QQ 桥接插件。

它适合这种场景：
- 你已经在本机跑着 Hermes Agent
- 你已经用 NapCat 登录了自己的 QQ
- 你希望通过 QQ 私聊或 QQ 群直接和 Hermes 说话

当前仓库实现特点：
- 入站：NapCat OneBot WebSocket Server（纯 WS）
- 出站：NapCat OneBot HTTP API
- 每个聊天独立 Hermes session，自动恢复 `--resume`
- 同聊天串行处理；处理中收到 follow-up 会中断并合并
- 支持文本、图片、语音、视频、文件
- 支持 `/new` `/reset` `/status` `/stop` `/help`
- 支持群文件上传 notice、离线文件、在线文件（NapCat 扩展能力）

---

## 1. 整体架构

```text
QQ 用户 / QQ 群
   ↓
NapCat（登录你的 QQ）
   ↓  OneBot WebSocket Server
Hermes QQ Bridge（本仓库）
   ↓  Hermes CLI / Hermes Agent
Hermes
   ↓  OneBot HTTP API
NapCat
   ↓
QQ 用户 / QQ 群
```

职责分工：
- NapCat：负责和 QQ 通讯
- 本插件：负责把 QQ 消息变成 Hermes 可处理的会话请求
- Hermes：负责理解、工具调用、生成回复

---

## 2. 私聊和群聊是怎么“接通”的

### 私聊

当你把 QQ 号加入白名单后：
- 你给机器人账号发私聊消息
- 桥会把这个私聊映射成一个独立 Hermes session
- 之后同一私聊会自动复用这个 session

也就是说：
- 私聊 A 和私聊 B 不会串上下文
- 你重启桥后，只要 session 还在，就会继续 `--resume`

### 群聊

群聊有两层控制：

1. 群号是否允许
2. 群里什么消息会触发

默认推荐行为：
- 只有这两种消息会触发机器人：
  - `@机器人`
  - 回复机器人上一条消息

如果你想让机器人在群里看见所有消息都回复：
- 配 `group_chat_all=true`

群聊 session 行为：
- 默认按“群 + 发言用户”隔离
- 也就是同一个群里，不同人通常是不同上下文
- 这样最接近 Hermes 原生 gateway 的默认共享聊天策略

---

## 3. 环境要求

必须满足：

1. Linux / WSL2 / 能稳定跑 Hermes 的环境
2. 已安装 Hermes Agent
3. 已安装并登录 NapCat
4. Python 依赖：
   - `requests`
   - `websockets`

如果你是用 Hermes 自带 venv 跑，通常已经有这些依赖。

---

## 4. 安装

```bash
git clone git@github.com:zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
bash scripts/install-plugin.sh
```

安装后会复制到：

```text
~/.hermes/plugins/napcat_qq_bridge
```

配置文件默认目录：

```text
~/.hermes/napcat_qq_bridge/config.json
```

---

## 5. 先配 NapCat

你至少要开两个服务：

### 5.1 HTTP Server

给桥发消息用。

推荐：
- Host: `127.0.0.1`
- Port: `3000`
- Token: 你自己的 token
- `messagePostFormat: array`

### 5.2 WebSocket Server

给桥推消息用。

推荐：
- Host: `127.0.0.1` 或 `0.0.0.0`
- Port: `3001`
- Access Token: 和 HTTP token 保持一致更省事

### 5.3 HTTP Client

建议关闭。

因为当前桥主方案是纯 WS 入站。
如果你同时开 HTTP Client webhook，容易双路重复投递。

---

## 6. 配置桥

先复制示例配置：

```bash
mkdir -p ~/.hermes/napcat_qq_bridge
cp napcat_qq_bridge/config.example.json ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json
```

### 6.1 一个可直接参考的配置

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
    "poll_interval": 0,
    "poll_history_count": 20,
    "poll_backfill_seconds": 600,
    "ws_reconnect_delay": 3,
    "request_timeout": 60,
    "chunk_size": 65536,
    "temp_dir": "/home/YOUR_USER/.hermes/napcat_qq_bridge/tmp",
    "state_dir": "/home/YOUR_USER/.hermes/napcat_qq_bridge/state"
  },
  "auth": {
    "private_users": ["YOUR_QQ_NUMBER"],
    "group_ids": ["YOUR_GROUP_ID"],
    "group_users": []
  },
  "hermes": {
    "bin": "hermes",
    "workdir": "/home/YOUR_USER",
    "model": "",
    "provider": "",
    "toolsets": "terminal,file,web",
    "skills": []
  }
}
```

### 6.2 关键字段解释

#### `onebot.url`
NapCat HTTP API 地址。
桥给 QQ 发消息时会用它。

#### `onebot.ws_url`
NapCat WebSocket Server 地址。
桥收 QQ 消息时会主动连上它。

#### `auth.private_users`
允许触发机器人的私聊 QQ 号列表。

如果你只想允许自己私聊控制 Hermes，就只填你自己的 QQ 号。

#### `auth.group_ids`
允许机器人工作的群号列表。

群不在这里，就算有人 @ 它也不会处理。

#### `auth.group_users`
可选的“群成员白名单”。

含义是：
- 只允许这些用户在群里触发机器人
- 适合把群开放给少数几个人用

#### `bridge.group_chat_all`
是否让群里所有消息都触发。

推荐默认：`false`

也就是只响应：
- `@机器人`
- 回复机器人上一条消息

如果你设成 `true`：
- 群里所有消息都可能触发
- 容易刷屏，不建议大群使用

#### `bridge.poll_interval`
默认建议保留 `0` 或非常低频，仅做辅助手段。
主入站通道还是 WebSocket。

#### `hermes.toolsets`
传给 Hermes 的 toolsets。

例如：
- `terminal,file,web`

#### `hermes.skills`
预加载 skills。

例如：
- `obsidian-schedule`
- `podcast-transcript`

---

## 7. 怎么让“私聊能用”

最简单做法：

1. 在 `auth.private_users` 里填入你的 QQ 号
2. 启动桥
3. 用这个 QQ 号去私聊 NapCat 登录的机器人账号

例如：

```json
"auth": {
  "private_users": ["YOUR_QQ_NUMBER"],
  "group_ids": [],
  "group_users": []
}
```

这样就只有你私聊时能触发。

---

## 8. 怎么让“群里能用”

### 推荐做法：只在指定群里，且只响应 @ 或 reply

```json
"auth": {
  "private_users": ["YOUR_QQ_NUMBER"],
  "group_ids": ["YOUR_GROUP_ID"],
  "group_users": []
},
"bridge": {
  "group_chat_all": false
}
```

效果：
- 机器人只在群 `YOUR_GROUP_ID` 工作
- 群里必须 `@机器人` 或回复机器人消息才触发

### 如果群里只想让少数人用

```json
"auth": {
  "private_users": ["YOUR_QQ_NUMBER"],
  "group_ids": ["YOUR_GROUP_ID"],
  "group_users": ["YOUR_QQ_NUMBER", "123456789"]
}
```

效果：
- 群本身允许
- 但只有 `group_users` 里的成员能触发

### 如果你真想让群里所有消息都触发

```json
"bridge": {
  "group_chat_all": true
}
```

不建议默认这么开。

---

## 9. 启动

### 直接启动

```bash
hermes napcat-qq-bridge run --config-file ~/.hermes/napcat_qq_bridge/config.json
```

或者直接：

```bash
hermes napcat-qq-bridge run
```

默认就会读：

```text
~/.hermes/napcat_qq_bridge/config.json
```

### systemd 启动

参考：

```text
examples/systemd/hermes-napcat-qq-bridge.service
```

启用示例：

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/hermes-napcat-qq-bridge.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/hermes-napcat-qq-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-napcat-qq-bridge.service
```

---

## 10. 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

你应该重点看这些字段：
- `receive_mode`
- `webhook_ingress_enabled`
- `websocket_connected`
- `websocket_last_error`
- `seen_events`
- `bot_user_id`
- `bot_name`

纯 WS 正常时通常应看到：
- `receive_mode = "ws"`
- `webhook_ingress_enabled = false`
- `websocket_connected = true`

---

## 11. 聊天命令

当前支持：

### `/new` / `/reset`
- 重置当前聊天会话
- 下一条消息会开启新对话

### `/status`
- 查看当前聊天是否在处理中
- 查看是否有排队消息
- 查看当前绑定的 session_id

### `/stop`
- 停掉当前正在处理的 Hermes 请求
- 不会删除 session

### `/help`
- 返回命令帮助

---

## 12. Hermes 回媒体的规则

如果 Hermes 想给 QQ 发本地文件，需要这样返回：

### 发图片 / 文件 / 视频 / 音频

```text
MEDIA:/absolute/path/to/file
```

### 如果音频要按“语音消息”发，而不是普通文件

```text
[[audio_as_voice]]
MEDIA:/absolute/path/to/audio.ogg
```

桥会自动根据扩展名分发：
- 图片 -> 图片消息
- 音频 -> 语音或文件
- 视频 -> 视频消息
- 其他 -> 文件消息

---

## 13. 路径和状态文件

### 临时目录
默认：

```text
~/.hermes/napcat_qq_bridge/tmp
```

### session 状态目录
默认：

```text
~/.hermes/napcat_qq_bridge/state
```

### session 文件

```text
~/.hermes/napcat_qq_bridge/state/sessions.json
```

---

## 14. 常见排错

### 14.1 桥启动失败

先看是否是 NapCat HTTP API 不通。

测试：

```bash
curl -X POST \
  http://127.0.0.1:3000/get_login_info \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

### 14.2 私聊没回复

检查：
1. 你的 QQ 号是否在 `auth.private_users`
2. NapCat 是否真的登录成功
3. `healthz` 里 `websocket_connected` 是否为 `true`

### 14.3 群里没回复

检查：
1. 群号是否在 `auth.group_ids`
2. 是否确实 `@机器人` 或回复机器人消息
3. 是否配了 `group_users`，导致你不在允许名单里
4. 如要排查，可暂时设 `group_chat_all=true`

### 14.4 图片/文件/语音收不到

检查：
1. NapCat 版本是否支持相关接口
2. NapCat token 是否正确
3. HTTP API 是否可访问
4. 文件资源是否已经过期

### 14.5 WebSocket 会偶尔断开

当前桥会自动重连，并在重连后做一次历史补拉。
如果你现场网络经常抖：
- 保持 `ws_reconnect_delay` 合理
- 不要同时开 HTTP Client webhook 双路投递

---

## 15. 开发与测试

```bash
cd hermes-qq-plugin
python -m pytest -q
```

如果你只想跑主要测试：

```bash
python -m pytest tests/test_napcat_qq_bridge.py -q
```

---

## 16. 仓库内主要文件

```text
.
├── napcat_qq_bridge/
│   ├── bridge.py                # 主逻辑
│   ├── cli.py                   # Hermes 命令注册
│   ├── config.example.json      # 示例配置
│   ├── README.md                # 组件级说明
│   └── plugin.yaml
├── tests/
├── examples/
│   ├── docker-compose.napcat.yml
│   └── systemd/hermes-napcat-qq-bridge.service
└── scripts/install-plugin.sh
```

---

## 17. 说明

这个仓库是 NapCat / OneBot 个人 QQ 的桥接插件仓库。

如果你用的是腾讯官方 QQ Bot OpenAPI，那不是这个仓库，对应的是 Hermes 主仓库里的 `qqbot` 平台适配器。

---

## License

MIT
