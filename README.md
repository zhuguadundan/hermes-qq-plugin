# Hermes QQ 个人号插件（NapCat / OneBot）

> 把 **QQ 个人号** 接进 Hermes Agent：适合家庭群、朋友群、私聊里的 AI 助手。  
> 它不是腾讯官方 QQ Bot API，而是通过 **NapCat + OneBot** 使用个人号收发消息。

## 最短安装卡片

```bash
# 1) 安装 Hermes Agent
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc  # 或 source ~/.zshrc

# 2) 下载并安装本插件
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
bash scripts/install-plugin.sh

# 3) 配置 NapCat / OneBot / QQ 白名单
# install-plugin.sh 会自动生成 ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json

# 4) 启动 QQ 个人号桥接
hermes napcat-qq-bridge run

# 5) 健康检查
curl http://127.0.0.1:8096/healthz
```

Systemd 用户服务示例：

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/hermes-napcat-qq-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-napcat-qq-bridge.service
systemctl --user status hermes-napcat-qq-bridge.service
```

---

## 这个插件解决什么问题？

Hermes 已经有官方 `qqbot` 平台，但 `qqbot` 面向腾讯官方 Bot API。很多个人场景，尤其是**家庭群助手**，更需要“一个正常 QQ 个人号”进入群聊：

- 家庭群里直接 @ 它问事、记事、查资料；
- 不需要把家人迁移到新平台；
- 群聊上下文可以共享，适合连续对话；
- 私聊也能作为自己的随身 Hermes 入口。

所以本仓库的语义是：

- **QQ Personal**：本插件，NapCat / OneBot / QQ 个人号桥接；
- **QQBot API**：Hermes 官方 `qqbot` 平台，腾讯官方 Bot API。

二者应该被明确区分，避免 Hermes 把个人号桥接和官方 QQ Bot 混在一起。

---

## 功能能力

当前版本已对齐 Hermes 原生消息平台的主要流程：

- NapCat OneBot **WebSocket 入站**；
- NapCat OneBot **HTTP API 出站**；
- QQ 私聊、QQ群聊；
- 文本、图片、语音、视频、普通文件、在线文件；
- 每个聊天绑定 Hermes session，自动 `--resume`；
- 处理中收到 follow-up 时，可中断当前轮次并合并新消息；
- 群聊 session 可选：
  - `group_sessions_per_user: true`：按群成员隔离；
  - `group_sessions_per_user: false`：整群共享一个上下文；
- 支持桥接本地命令：
  - `/new`
  - `/reset`
  - `/status`
  - `/stop`
  - `/help`
- 支持 Hermes slash 命令透传：
  - `/reasoning`
  - `/model`
  - `/fast`
  - `/usage`
  - `/compress`
  - 以及其他 Hermes 已注册命令；
- `/new`、`/reset`、`/model`、`/reasoning`、`/fast` 会显示模型信息块，例如：

```text
◆ Model: gpt-5.5
◆ Provider: custom
◆ Context: 200K tokens 配置
```

---

## 仓库结构

```text
.
├── README.md
├── examples/
│   ├── docker-compose.napcat.yml
│   └── systemd/hermes-napcat-qq-bridge.service
├── napcat_qq_bridge/
│   ├── __init__.py
│   ├── bridge.py
│   ├── cli.py
│   ├── config.example.json
│   ├── plugin.yaml
│   └── README.md
├── scripts/install-plugin.sh
└── tests/test_napcat_qq_bridge.py
```

---

## 安装前准备

你需要先准备：

1. Hermes Agent 已安装并能正常运行；
2. NapCat 已登录 QQ；
3. NapCat 已开启 OneBot HTTP API 和 WebSocket Server；
4. 知道你的 QQ 号、要接入的群号。

建议先确认 Hermes 本体能正常对话：

```bash
hermes
```

---

## 配置文件

默认配置路径：

```text
~/.hermes/napcat_qq_bridge/config.json
```

`bash scripts/install-plugin.sh` 会从 `napcat_qq_bridge/config.example.json` 生成这个文件，并自动把示例里的 `/home/YOUR_USER` 替换为当前用户的 `$HOME`。

最重要的是下面几段：

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
    "receive_mode": "ws",
    "group_chat_all": false,
    "group_sessions_per_user": false,
    "enable_online_file": true,
    "auto_approve_dangerous_commands": false
  },
  "auth": {
    "private_users": ["你的QQ号"],
    "group_ids": ["家庭群号"],
    "group_users": []
  },
  "hermes": {
    "bin": "hermes",
    "workdir": "/home/你的用户名",
    "model": "",
    "provider": "",
    "toolsets": "terminal,file,web",
    "skills": []
  }
}
```

### 家庭群助手推荐配置

如果你希望一个家庭群共享同一段上下文，推荐：

```json
"group_chat_all": false,
"group_sessions_per_user": false
```

含义：

- `group_chat_all: false`：默认只响应 @机器人 或回复机器人，避免群里所有闲聊都触发；
- `group_sessions_per_user: false`：同一个群共享一个 Hermes session，适合家庭助手连续对话。

如果你希望群里每个人都有独立上下文，把 `group_sessions_per_user` 改成 `true`。

---

## 启动方式

前台启动：

```bash
hermes napcat-qq-bridge run
```

指定配置文件：

```bash
hermes napcat-qq-bridge run --config-file ~/.hermes/napcat_qq_bridge/config.json
```

Systemd 后台运行见：

```text
examples/systemd/hermes-napcat-qq-bridge.service
```

---

## 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

重点看这些字段：

- `ok: true`
- `websocket_connected: true`
- `bot_user_id`
- `bot_name`
- `allowed_private_users`
- `allowed_groups`
- `group_chat_all`
- `group_sessions_per_user`
- `enable_online_file`
- `auto_approve_dangerous_commands`

---

## 常用 QQ 命令

桥接本地命令：

```text
/new      重置当前聊天 session，并显示模型 / provider / context
/reset    同 /new
/status   查看桥接状态
/stop     停止当前处理中任务
/help     查看帮助
```

Hermes 命令透传示例：

```text
/reasoning
/reasoning high
/reasoning xhigh
/model
/model gpt-5.5
/fast
/usage
/compress
```

注意：QQ 里没有 Hermes TUI 的交互式选择器，所以 `/model` 不会打开选择菜单；请使用 `/model <模型名>`。

---

## 媒体回发

Hermes 最终回复里包含下面格式时，桥会把文件回发到 QQ：

```text
MEDIA:/absolute/path/to/file.png
```

音频按 QQ 语音发送：

```text
[[audio_as_voice]]
MEDIA:/absolute/path/to/audio.ogg
```

---

## 对 Hermes 本体需要什么改动？

### 最小运行：不需要改 Hermes 本体源码

只要 Hermes CLI 正常可用，本插件可以通过结构化调用运行 Hermes，并把结果发回 QQ。

### 推荐增强：让 Web UI / status 也识别 QQ Personal

如果你希望 Hermes Web UI 和状态页里明确显示 **QQ Personal**，并和官方 **QQBot API** 分开，需要在 Hermes 本体做集成增强。改动方向如下：

1. `gateway/config.py`
   - 增加 `platforms.qq` / `QQ Personal` 的独立识别；
   - 保留 `platforms.qqbot` 给官方 QQ Bot API；
   - 可显式配置 `platforms.qqbot.enabled: false`，避免误启官方 QQ Bot。
2. `hermes_cli/gateway.py`
   - 增加 QQ Personal bridge 的 setup / runtime status helper；
   - 能读取 systemd / healthz 状态。
3. `hermes_cli/status.py`
   - 分开展示 `QQ Personal` 和 `QQBot API`。
4. `hermes_cli/web_server.py`
   - `/api/status` 注入 `qq_personal` 状态。
5. Web UI 前端
   - platform card / sessions / alert 里渲染 `QQ Personal`。

这些增强不是本插件启动的硬依赖，但能让体验更接近 Hermes 原生消息平台。

---

## 与官方 QQ Bot 的区分建议

推荐 Hermes 配置中这样表达：

```yaml
platforms:
  qqbot:
    enabled: false   # 官方腾讯 QQ Bot API，不使用就关掉
  qq:
    enabled: true    # QQ Personal / NapCat / OneBot / 个人号
```

本插件自身仍通过 `~/.hermes/napcat_qq_bridge/config.json` 运行；上面的 Hermes 配置主要用于 Web UI / status / setup 层面的区分展示。

---

## 开发与测试

```bash
python -m py_compile napcat_qq_bridge/bridge.py
python -m pytest -q tests/test_napcat_qq_bridge.py
```

当前测试覆盖：

- OneBot 消息解析；
- 私聊 / 群聊触发；
- session reset；
- follow-up 合并；
- 文件 / 图片 / 语音 / 在线文件处理；
- `/new` 模型信息输出；
- Hermes slash 命令透传。

---

## 安全提醒

- 不要公开你的 NapCat token；
- 不要把个人 QQ 号部署在不可信机器上；
- 默认建议使用 allowlist；
- `auto_approve_dangerous_commands: true` 等同于 QQ 会话里自动批准危险命令，只建议在完全可信环境里开启。

---

## License

见 `LICENSE`。
