# Hermes QQ Plugin

这是一个把 NapCat（个人 QQ / NTQQ）接到 Hermes 的 QQ 桥接插件。

它适合这样的场景：
- 你已经能在本机正常运行 Hermes Agent
- 你已经让 NapCat 登录了自己的 QQ
- 你希望通过 QQ 私聊或 QQ 群直接和 Hermes 对话

当前实现特点：
- 入站：NapCat OneBot WebSocket Server（纯 WS）
- 出站：NapCat OneBot HTTP API
- 每个聊天独立 Hermes session，自动恢复 `--resume`
- 同一聊天串行处理；处理中收到 follow-up 会中断并合并
- 支持文本、图片、语音、视频、普通文件
- 支持 `/new` `/reset` `/status` `/stop` `/help`
- 支持群文件上传 notice、离线文件、在线文件（NapCat 扩展）
- 回复优先走 Hermes 结构化最终结果，不依赖 CLI stdout 抓正文

---

## 1. 目录说明

这个插件涉及 3 个不同位置，先分清：

### 1.1 GitHub 源码仓库
就是你 clone 下来的仓库目录，例如：

```text
~/hermes-qq-plugin
```

这里只是源码，不会被 Hermes 自动加载。

### 1.2 插件安装目录
Hermes 实际加载的插件代码目录：

```text
~/.hermes/plugins/napcat_qq_bridge
```

`scripts/install-plugin.sh` 会把仓库里的 `napcat_qq_bridge/` 复制到这里。

### 1.3 插件运行目录
运行配置、缓存、状态目录：

```text
~/.hermes/napcat_qq_bridge
```

这里通常会有：
- `config.json`
- `tmp/`
- `state/`

记忆方法：
- 仓库目录 = 源码
- `~/.hermes/plugins/...` = Hermes 加载的插件代码
- `~/.hermes/napcat_qq_bridge/...` = 配置和运行数据

---

## 2. 架构

```text
QQ 用户 / QQ 群
   ↓
NapCat（登录你的 QQ）
   ↓  OneBot WebSocket Server
Hermes QQ Bridge（本插件）
   ↓  Hermes CLI / Hermes Agent
Hermes
   ↓  OneBot HTTP API
NapCat
   ↓
QQ 用户 / QQ 群
```

职责分工：
- NapCat：负责和 QQ 通讯
- 本插件：负责把 QQ 消息转成 Hermes 会话请求
- Hermes：负责理解、调用工具、生成回复

---

## 2.5 Hermes 官方并没有内置支持这个 QQ 插件

这点要说清楚：

- 这个仓库是 NapCat / OneBot 个人 QQ 的桥接插件仓库
- 不是 Hermes 官方内置发布物
- 如果你想获得“像原生消息渠道一样稳定”的体验，通常还需要给 Hermes 本体打补丁

补丁清单我单独整理在：

- `docs/HERMES_PATCH_CHECKLIST.md`

如果你是第一次部署，强烈建议把那份文档也一起看完。

---

## 3. 环境要求

必须满足：

1. Linux / WSL2 / 其他能稳定运行 Hermes 的环境
2. 已安装 Hermes Agent
3. 已安装并登录 NapCat
4. Python 依赖可用：
   - `requests`
   - `websockets`

如果你是使用 Hermes 自带 venv 启动，一般这些依赖已经具备。

---

## 4. 安装步骤

### 4.1 克隆仓库

```bash
git clone git@github.com:zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
```

### 4.2 安装插件到 Hermes

```bash
bash scripts/install-plugin.sh
```

安装脚本会做两件事：
- 把插件代码复制到 `~/.hermes/plugins/napcat_qq_bridge`
- 如果 `~/.hermes/napcat_qq_bridge/config.json` 还不存在，就自动从示例配置生成一份

安装完成后：
- 插件代码目录：`~/.hermes/plugins/napcat_qq_bridge`
- 运行目录：`~/.hermes/napcat_qq_bridge`
- 默认配置文件：`~/.hermes/napcat_qq_bridge/config.json`

### 4.3 确认 Hermes 能看到插件

```bash
hermes napcat-qq-bridge --help
```

如果这条命令能正常输出帮助，说明插件已被 Hermes 正确加载。

---

## 5. 先配置 NapCat

你至少要打开两个服务。

### 5.1 HTTP Server

给桥发消息用。

推荐配置：
- Host: `127.0.0.1`
- Port: `3000`
- Token: 你自己的 token
- `messagePostFormat: array`

### 5.2 WebSocket Server

给桥推消息用。

推荐配置：
- Host: `127.0.0.1` 或 `0.0.0.0`
- Port: `3001`
- Access Token: 建议和 HTTP token 保持一致

### 5.3 HTTP Client

建议关闭。

原因：
- 当前桥主方案是纯 WS 入站
- 如果同时打开 HTTP Client webhook，容易双路重复投递

---

## 6. 配置桥

### 6.1 准备配置文件

```bash
mkdir -p ~/.hermes/napcat_qq_bridge
cp napcat_qq_bridge/config.example.json ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json
```

### 6.2 示例配置

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

### 6.3 关键字段解释

#### `onebot.url`
NapCat HTTP API 地址。
桥给 QQ 发消息时会使用它。

#### `onebot.ws_url`
NapCat WebSocket Server 地址。
桥收 QQ 消息时会主动连上它。

#### `auth.private_users`
允许触发机器人的私聊 QQ 号列表。

#### `auth.group_ids`
允许机器人工作的群号列表。

#### `auth.group_users`
可选群成员白名单。

如果不为空，则只有这些成员可以在群里触发机器人。

#### `bridge.group_chat_all`
是否让群里所有消息都触发。

推荐默认：`false`

也就是只响应：
- `@机器人`
- 回复机器人上一条消息

如果设成 `true`：
- 群里所有消息都可能触发
- 容易刷屏，不建议默认开启

#### `bridge.poll_interval`
轮询兜底间隔。

主通道仍然是 WebSocket。
如果你非常依赖纯 WS，可以保持 `0`。

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

## 7. 私聊怎么接通

最简单的方式：

```json
"auth": {
  "private_users": ["YOUR_QQ_NUMBER"],
  "group_ids": [],
  "group_users": []
}
```

这样：
- 只有这个 QQ 号私聊机器人时能触发
- 会形成一个独立 Hermes session
- 同一私聊会自动复用 session

---

## 8. 群聊怎么接通

### 推荐做法：指定群 + 只响应 @ / reply

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

### 如果你想让群里所有消息都触发

```json
"bridge": {
  "group_chat_all": true
}
```

不建议默认这么开。

---

## 9. 启动方式

### 9.1 直接启动

```bash
hermes napcat-qq-bridge run --config-file ~/.hermes/napcat_qq_bridge/config.json
```

或者：

```bash
hermes napcat-qq-bridge run
```

默认会读取：

```text
~/.hermes/napcat_qq_bridge/config.json
```

### 9.2 systemd 启动

仓库里有示例：

```text
examples/systemd/hermes-napcat-qq-bridge.service
```

启用方式：

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/hermes-napcat-qq-bridge.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/hermes-napcat-qq-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-napcat-qq-bridge.service
```

---

## 10. 首次启动后的最小自检清单

建议按这个顺序检查：

1. 插件命令是否注册成功

```bash
hermes napcat-qq-bridge --help
```

2. NapCat HTTP API 是否可达

```bash
curl -X POST \
  http://127.0.0.1:3000/get_login_info \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

3. 启动桥

```bash
hermes napcat-qq-bridge run
```

4. 检查健康接口

```bash
curl http://127.0.0.1:8096/healthz
```

5. 用白名单 QQ 做一次私聊测试
6. 在允许的群里做一次 `@机器人` 测试

如果这 6 步都过了，基本就不用再盲目 debug 了。

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

作用：
- `/new` / `/reset`：重置当前聊天 session
- `/status`：查看当前聊天状态和 session
- `/stop`：停止当前处理
- `/help`：返回命令说明

---

## 13. 媒体回复规则

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

桥会按扩展名自动分发：
- 图片 -> 图片消息
- 音频 -> 语音或文件
- 视频 -> 视频消息
- 其他 -> 文件消息

---

## 14. 常见排错

### 13.1 私聊没回复

检查：
1. 你的 QQ 号是否在 `auth.private_users`
2. NapCat 是否真的登录成功
3. `healthz` 里 `websocket_connected` 是否为 `true`

### 13.2 群里没回复

检查：
1. 群号是否在 `auth.group_ids`
2. 是否确实 `@机器人` 或回复机器人消息
3. 是否配了 `group_users`，导致你不在允许名单里
4. 如要排查，可暂时设 `group_chat_all=true`

### 13.3 桥启动失败

先测 NapCat HTTP API：

```bash
curl -X POST \
  http://127.0.0.1:3000/get_login_info \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

### 13.4 图片 / 文件 / 语音收不到

检查：
1. NapCat 版本是否支持相关接口
2. NapCat token 是否正确
3. HTTP API 是否可访问
4. 文件资源是否已经过期

### 13.5 WebSocket 偶尔断开

当前桥会自动重连，并在重连后做一次历史补拉。
如果现场网络经常抖：
- 保持 `ws_reconnect_delay` 合理
- 不要同时开 HTTP Client webhook 双路投递

---

## 15. 测试

```bash
cd hermes-qq-plugin
/home/dawei/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_napcat_qq_bridge.py -q
```

---

## 16. 仓库结构

```text
.
├── napcat_qq_bridge/
│   ├── bridge.py
│   ├── cli.py
│   ├── config.example.json
│   ├── README.md
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
