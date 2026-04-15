# Hermes QQ Plugin

一个把 NapCat（个人 QQ / NTQQ）接到 Hermes 的 QQ 桥接插件。

适合这种场景：
- 你已经在本机跑着 Hermes Agent
- 你已经用 NapCat 登录了自己的 QQ
- 你希望通过 QQ 私聊或 QQ 群直接和 Hermes 说话

当前桥接实现：
- 入站：NapCat OneBot WebSocket Server（纯 WS）
- 出站：NapCat OneBot HTTP API
- 每个聊天独立 Hermes session，自动恢复 `--resume`
- 同聊天串行处理；处理中收到 follow-up 会中断并合并
- 支持文本、图片、语音、视频、文件
- 支持 `/new` `/reset` `/status` `/stop` `/help`
- 支持群文件上传 notice、离线文件、在线文件（NapCat 扩展能力）
- 回复优先走 Hermes 结构化最终结果，不再依赖 CLI stdout 抓正文

---

## 1. 先分清三个位置

这个仓库会涉及 3 个位置，但它们含义不同：

1. GitHub 源码仓库
   - 你 clone 下来的项目目录
   - 例如：`~/hermes-qq-plugin`
   - 这里只是源码，不会被 Hermes 自动加载

2. 插件安装目录
   - `~/.hermes/plugins/napcat_qq_bridge`
   - 这里是 Hermes 实际加载的插件代码
   - `scripts/install-plugin.sh` 就是把源码复制到这里

3. 插件运行目录
   - `~/.hermes/napcat_qq_bridge`
   - 这里放运行配置和状态：
     - `config.json`
     - `tmp/`
     - `state/`

记住就行：
- 源码看仓库
- Hermes 实际加载代码看 `~/.hermes/plugins/napcat_qq_bridge`
- 配置和状态看 `~/.hermes/napcat_qq_bridge`

---

## 2. 整体架构

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
- 本插件：负责把 QQ 消息变成 Hermes 会话请求
- Hermes：负责理解、工具调用、生成回复

---

## 3. 私聊和群聊怎么接通

### 私聊

当你把某个 QQ 号加入白名单后：
- 这个 QQ 号私聊机器人账号
- 桥会把该私聊映射成一个独立 Hermes session
- 之后同一私聊会自动复用这个 session

结果就是：
- 私聊 A 和私聊 B 不会串上下文
- 重启桥后，只要 session 还在，就会继续 `--resume`

### 群聊

群聊有两层控制：
1. 群号是否允许
2. 群里什么消息会触发

默认推荐行为：
- 只有这两种消息会触发机器人：
  - `@机器人`
  - 回复机器人上一条消息

如果你想让机器人在群里看见所有消息都回复：
- 设 `group_chat_all=true`

群聊 session 行为：
- 默认按“群 + 发言用户”隔离
- 同一个群里，不同人通常是不同上下文

---

## 4. 环境要求

必须满足：
1. Linux / WSL2 / 能稳定跑 Hermes 的环境
2. 已安装 Hermes Agent
3. 已安装并登录 NapCat
4. Python 依赖：
   - `requests`
   - `websockets`

如果你是用 Hermes 自带 venv 跑，通常已经有这些依赖。

---

## 5. 安装

```bash
git clone git@github.com:zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
bash scripts/install-plugin.sh
```

安装完成后：
- 插件代码会被复制到：
  - `~/.hermes/plugins/napcat_qq_bridge`
- 运行配置仍然放在：
  - `~/.hermes/napcat_qq_bridge/config.json`

你可以这样确认插件命令已注册：

```bash
hermes napcat-qq-bridge --help
```

---

## 6. 先配 NapCat

你至少要开两个服务。

### 6.1 HTTP Server
给桥发消息用。

推荐：
- Host: `127.0.0.1`
- Port: `3000`
- Token: 你自己的 token
- `messagePostFormat: array`

### 6.2 WebSocket Server
给桥推消息用。

推荐：
- Host: `127.0.0.1` 或 `0.0.0.0`
- Port: `3001`
- Access Token: 和 HTTP token 保持一致更省事

### 6.3 HTTP Client
建议关闭。

因为当前桥主方案是纯 WS 入站。
如果你同时开 HTTP Client webhook，容易双路重复投递。

---

## 7. 配置桥

先复制示例配置：

```bash
mkdir -p ~/.hermes/napcat_qq_bridge
cp napcat_qq_bridge/config.example.json ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json
```

### 7.1 一个可直接参考的配置

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

### 7.2 关键字段解释

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

---

## 8. 怎么让私聊能用

最简单做法：

```json
"auth": {
  "private_users": ["YOUR_QQ_NUMBER"],
  "group_ids": [],
  "group_users": []
}
```

这样就只有你私聊时能触发。

---

## 9. 怎么让群里能用

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
- 机器人只在指定群里工作
- 群里必须 `@机器人` 或回复机器人消息才触发

### 如果群里只想让少数人用

```json
"auth": {
  "private_users": ["YOUR_QQ_NUMBER"],
  "group_ids": ["YOUR_GROUP_ID"],
  "group_users": ["YOUR_QQ_NUMBER", "ANOTHER_ALLOWED_QQ_NUMBER"]
}
```

### 如果你真想让群里所有消息都触发

```json
"bridge": {
  "group_chat_all": true
}
```

不建议默认这么开。

---

## 10. 启动

### 直接启动

```bash
hermes napcat-qq-bridge run --config-file ~/.hermes/napcat_qq_bridge/config.json
```

或者：

```bash
hermes napcat-qq-bridge run
```

默认会读取：
- `~/.hermes/napcat_qq_bridge/config.json`

### systemd 启动

参考：
- `examples/systemd/hermes-napcat-qq-bridge.service`

启用示例：

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/hermes-napcat-qq-bridge.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/hermes-napcat-qq-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-napcat-qq-bridge.service
```

---

## 11. 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

重点看这些字段：
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

## 12. 聊天命令

支持：
- `/new` / `/reset`
- `/status`
- `/stop`
- `/help`

---

## 13. Hermes 回媒体的规则

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

## 14. 常见排错

### 私聊没回复
检查：
1. 你的 QQ 号是否在 `auth.private_users`
2. NapCat 是否真的登录成功
3. `healthz` 里 `websocket_connected` 是否为 `true`

### 群里没回复
检查：
1. 群号是否在 `auth.group_ids`
2. 是否确实 `@机器人` 或回复机器人消息
3. 是否配了 `group_users`，导致你不在允许名单里
4. 如要排查，可暂时设 `group_chat_all=true`

### 桥启动失败
测试 NapCat HTTP API：

```bash
curl -X POST \
  http://127.0.0.1:3000/get_login_info \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

### 图片/文件/语音收不到
检查：
1. NapCat 版本是否支持相关接口
2. NapCat token 是否正确
3. HTTP API 是否可访问
4. 文件资源是否已经过期

---

## 15. 测试

```bash
cd hermes-qq-plugin
/home/dawei/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_napcat_qq_bridge.py -q
```

---

## 16. 说明

这个仓库是 NapCat / OneBot 个人 QQ 的桥接插件仓库。

如果你用的是腾讯官方 QQ Bot OpenAPI，那不是这个仓库，对应的是 Hermes 主仓库里的 `qqbot` 平台适配器。

---

## License

MIT
