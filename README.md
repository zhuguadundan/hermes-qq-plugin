<<<<<<< HEAD
# Hermes QQ Plugin（已进入维护/迁移状态）

这个仓库原本用于把 NapCat（个人 QQ / NTQQ）桥接到 Hermes。

**现在推荐的方案已经不是这个独立插件，而是 Hermes 仓库里的原生 QQ 适配器。**

---

## 当前状态

- 本仓库：**保留为历史实现 / 回滚参考**
- 推荐运行方式：**Hermes Gateway 原生 QQ 适配器**
- 原生适配器位置：`gateway/platforms/qq.py`
- 官方 QQ Bot API v2 适配器仍然独立：`gateway/platforms/qqbot.py`

也就是说：

- 如果你要继续新部署 QQ 消息能力，优先用 **Hermes 原生 QQ 接入**
- 这个仓库不再是主线部署入口

---

## 为什么切换到原生 QQ 适配器

原生接入已经覆盖了这个插件最核心的能力：

- NapCat / OneBot 11 入站与出站
- QQ 私聊、QQ群、媒体消息、文件消息
- Gateway 统一会话管理
- 工具调用、cron 投递、模型配置、权限控制统一走 Hermes 主仓库

同时，原生接入已经修正了一个实际使用中很重要的问题：

### QQ 群聊现在可以共享上下文

通过在 Hermes 配置里设置：

```yaml
platforms:
  qq:
    enabled: true
    extra:
      onebot_url: http://127.0.0.1:3000
      onebot_ws_url: ws://127.0.0.1:3001
      group_sessions_per_user: false
```

即可让 **同一个 QQ 群内所有消息共享同一个会话上下文**，不再按发言人拆成独立 session。
Hermes 还会自动把入站消息前缀成 `[发送者] 消息内容`，从而维持多人连续对话。

---

## 迁移建议

如果你当前还在使用这个插件，建议迁移到 Hermes 主仓库的原生 QQ 方案：

1. 在 Hermes 主仓库使用 `platforms.qq`
2. 把 NapCat 的 HTTP / WebSocket 地址迁到 `~/.hermes/config.yaml`
3. 如需群共享上下文，设置 `platforms.qq.extra.group_sessions_per_user: false`
4. 使用 `hermes-gateway.service` 作为主运行时

迁移说明可参考 Hermes 主仓库中的：

- `docs/migration/qq-native-adapter.md`

---

## 这个仓库还保留什么价值

这个仓库仍然适合：

- 查看旧桥接实现细节
- 对比原生适配器迁移前后的行为
- 做历史回溯或紧急回滚参考

但它**不再建议作为默认部署入口**。

---

## 结论

如果你是第一次部署 QQ：

> **请直接使用 Hermes 原生 QQ 适配器，不要再从这个仓库开始。**

如果你已经在生产上跑这个插件：

> **建议迁移，并把这个仓库当作 legacy / archive 文档保存。**
=======
# Hermes NapCat QQ Personal Bridge

这是一个把 **NapCat / OneBot / QQ个人号** 接到 **Hermes Agent** 的桥接仓库。

> ## 最短安装卡片
> 
> ```bash
> # 1) 安装 Hermes Agent
> curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
> source ~/.bashrc
> 
> # 2) 安装本插件
> git clone <THIS_REPO_URL> legacy-qq-bridge-src
> cd legacy-qq-bridge-src
> bash scripts/install-plugin.sh
> 
> # 3) 配置 NapCat / OneBot
> $EDITOR ~/.hermes/napcat_qq_bridge/config.json
> 
> # 4) 启动
> hermes napcat-qq-bridge run
> 
> # 5) 健康检查
> curl http://127.0.0.1:8096/healthz
> ```
> 
> 如果你只想先跑起来，**不需要修改 Hermes 本体源码**。

它的目标不是腾讯官方 **QQ Bot API**，而是：

- 用你自己的 **QQ个人号** 和 Hermes 对话
- 支持 **私聊 / 群聊 / 图片 / 语音 / 视频 / 文件**
- 让 QQ 渠道尽可能接近 Hermes 原生消息平台的使用体验
- 在现代 Hermes 上保持可运行、可维护、可分享

> 推荐语义约定：
>
> - **QQ Personal** = 这个仓库 / NapCat / OneBot / 个人号桥接
> - **QQBot API** = Hermes 官方 `qqbot` 平台（腾讯官方 API）

---

## 1. 当前能力

这套桥接当前已经具备：

- NapCat OneBot **WebSocket Server 入站**
- NapCat OneBot **HTTP API 出站**
- 每个聊天绑定固定 Hermes session，自动 `--resume`
- follow-up 到来时可中断当前轮次并合并后续消息
- 支持文本、图片、语音、视频、普通文件、在线文件
- 支持桥接本地命令：
  - `/new`
  - `/reset`
  - `/status`
  - `/stop`
  - `/help`
- 支持 Hermes 命令透传：
  - `/reasoning`
  - `/model`
  - `/usage`
  - `/compress`
  - 以及其他 Hermes 已识别 slash 命令
- 群聊可切换：
  - **按人隔离 session**
  - **整群共享 session**
- 可开关：
  - `enable_online_file`
  - `auto_approve_dangerous_commands`
- 输出优先取 Hermes 结构化结果，不依赖 CLI stdout 抓正文

---

## 2. 仓库结构

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

## 3. 先说结论：Hermes 本体要不要改？

### 最小可用：**不需要改 Hermes 本体源码**

如果你只想让桥跑起来，答案是：

**不用改 Hermes 本体。**

你只需要：

1. 安装 Hermes Agent
2. 安装 NapCat 并登录 QQ
3. 安装这个插件
4. 启动 `hermes napcat-qq-bridge run`

### 想达到“像原生平台一样”的体验：**建议改 Hermes 本体**

如果你希望：

- Hermes Web UI 里能看到 **QQ Personal**
- `QQ Personal` 和官方 `QQBot API` 分开展示
- `hermes status` 和 dashboard 状态页里能看到它
- `platforms.qqbot.enabled: false` 时显式压住官方 QQ Bot
- setup/status/web UI 都更接近 Hermes 原生平台

那么建议你对 Hermes 本体做一组**集成增强补丁**。

下面 README 里我会把“最小必需”和“推荐增强”都写清楚。

---

## 4. 安装新版 Hermes Agent

优先按 Hermes 官方 README 安装。

### 4.1 快速安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc   # 或 ~/.zshrc
```

安装后先确认这些命令可用：

```bash
hermes --help
hermes status
hermes gateway status
```

### 4.2 开发者 / 手动安装

```bash
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
./setup-hermes.sh
./hermes
```

或：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"
```

> 只要你已经有可用的 `hermes` 命令，并且 `hermes status` 能跑通，本插件就可以继续接。

---

## 5. 安装本插件

### 5.1 克隆仓库

```bash
git clone <THIS_REPO_URL> legacy-qq-bridge-src
cd legacy-qq-bridge-src
```

### 5.2 安装到 Hermes

```bash
bash scripts/install-plugin.sh
```

安装脚本会：

- 把 `napcat_qq_bridge/` 复制到 `~/.hermes/plugins/napcat_qq_bridge`
- 如果 `~/.hermes/napcat_qq_bridge/config.json` 不存在，就自动生成一份默认配置

### 5.3 确认 Hermes 能识别命令

```bash
hermes napcat-qq-bridge --help
```

如果这条命令能正常输出帮助，说明插件已经被 Hermes 正确加载。

---

## 6. NapCat / OneBot 侧要求

你至少要准备两条链路：

### 6.1 OneBot HTTP API
用于 **桥 → QQ 发消息**。

推荐：

- Host: `127.0.0.1`
- Port: `3000`
- Token: 自己设置
- `messagePostFormat: array`

### 6.2 OneBot WebSocket Server
用于 **QQ → 桥 收消息**。

推荐：

- Host: `127.0.0.1` 或 `0.0.0.0`
- Port: `3001`
- Access Token: 建议与 HTTP token 一致

### 6.3 HTTP Client webhook

**建议关闭。**

当前桥推荐模式是：

- 入站：WebSocket Server
- 出站：HTTP API

这样不容易双路重复投递。

---

## 7. 配置桥

默认运行配置路径：

```bash
~/.hermes/napcat_qq_bridge/config.json
```

可直接从示例文件开始：

```bash
cp napcat_qq_bridge/config.example.json ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json
```

### 关键字段

#### `onebot.url`
NapCat HTTP API 地址。

#### `onebot.ws_url`
NapCat WebSocket Server 地址。

#### `auth.private_users`
允许触发机器人的私聊 QQ 号。

#### `auth.group_ids`
允许机器人工作的群号。

#### `bridge.group_chat_all`
群里是否所有消息都触发。

#### `bridge.group_sessions_per_user`
群会话是否按发言人隔离：

- `true`：同群不同人分开上下文
- `false`：整个群共用上下文

#### `bridge.enable_online_file`
是否自动接收 QQ 私聊在线文件。

#### `bridge.auto_approve_dangerous_commands`
是否让该 QQ 渠道默认进入 YOLO / 免审批模式。

---

## 8. 如何启动

### 8.1 前台直接启动（最推荐）

```bash
hermes napcat-qq-bridge run
```

如果用自定义配置路径：

```bash
hermes napcat-qq-bridge run --config-file ~/.hermes/napcat_qq_bridge/config.json
```

你应该能看到类似输出：

```text
NapCat QQ bridge listening on http://127.0.0.1:8096/napcat
Health check: http://127.0.0.1:8096/healthz
OneBot API: http://127.0.0.1:3000
Receive mode: ws
OneBot WebSocket: ws://127.0.0.1:3001
Bot self_id: ...
```

### 8.2 systemd 常驻（推荐生产/长期运行）

模板文件：

```text
examples/systemd/hermes-napcat-qq-bridge.service
```

安装到：

```bash
~/.config/systemd/user/hermes-napcat-qq-bridge.service
```

然后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable hermes-napcat-qq-bridge.service
systemctl --user restart hermes-napcat-qq-bridge.service
systemctl --user status hermes-napcat-qq-bridge.service
```

### 8.3 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

重点看：

- `ok`
- `websocket_connected`
- `bot_user_id`
- `bot_name`
- `seen_events`
- `group_sessions_per_user`
- `enable_online_file`
- `auto_approve_dangerous_commands`

---

## 9. QQ 里可用的命令行为

### 9.1 桥本地命令
这些由桥自己处理：

- `/new`
- `/reset`
- `/status`
- `/stop`
- `/help`

### 9.2 透传给 Hermes 的命令
这些会进入 Hermes 的命令处理流：

- `/reasoning`
- `/model`
- `/usage`
- `/compress`
- `/personality`
- 以及其他 Hermes 已识别 slash 命令

### 9.3 当前和原生平台仍有区别的地方

- `/model` 目前是 **文本 fallback**，不是交互式 picker
- `/status` 目前是桥本地版，不是 Hermes gateway 原生完整版状态文本
- richer UI（按钮审批、交互式 picker）仍不如 Telegram/Discord 原生 adapter

---

## 10. 如果你想让它和 Hermes 原生平台更一致，需要改 Hermes 本体哪些地方

下面这部分不是“桥运行必需”，而是“**想把体验做得像原生平台一样**”时推荐做的改动。

### 10.1 配置层区分 `QQ Personal` 和 `QQBot API`

建议改：

- `gateway/config.py`

目标：

- `platforms.qq` = QQ Personal / NapCat / OneBot / 个人号桥接
- `platforms.qqbot` = 官方 QQ Bot API

建议行为：

- 如果 `platforms.qqbot.enabled: false`，即使环境里有 `QQ_APP_ID / QQ_CLIENT_SECRET` 也不要自动启用官方 qqbot
- 如果检测到 `platforms.qq.extra.onebot_url / onebot_ws_url`，把它识别为 personal QQ bridge 配置

### 10.2 CLI setup 增加 `QQ Personal`

建议改：

- `hermes_cli/gateway.py`

目标：

- `hermes gateway setup` 里单独出现：
  - `QQ Personal (NapCat / OneBot)`
  - `QQ Bot (Official API)`

建议行为：

- personal QQ setup 写入 `config.yaml -> platforms.qq`
- 同时显式写入：
  - `platforms.qqbot.enabled: false`
- 再把 `platforms.qq` 同步成桥运行时 JSON 配置

### 10.3 `hermes status` 里单独显示 `QQ Personal`

建议改：

- `hermes_cli/status.py`

目标：

- `QQBot API`
- `QQ Personal`

分别显示，而不是混在一起。

### 10.4 Web UI / dashboard 里显示 `QQ Personal`

建议改：

- `hermes_cli/web_server.py`
- `web/src/lib/api.ts`
- `web/src/components/PlatformsCard.tsx`
- `web/src/pages/SessionsPage.tsx`

目标：

- `/api/status` 里合并 personal QQ bridge 运行态
- dashboard 能看到 `QQ Personal`
- 告警和 connected platforms 列表里正常显示

### 10.5 官方 qqbot 文档与注释不要再混写

建议改：

- `website/docs/user-guide/messaging/qqbot.md`
- `gateway/platforms/qqbot/adapter.py`

目标：

- qqbot 文档只谈官方 API
- personal QQ 只谈 bridge

---

## 11. 推荐的对外分享口径

如果你要把这套方案分享给别人，最不容易误导的说法是：

1. **Hermes 本体按官方方式安装**
2. **这个仓库只负责 QQ 个人号桥接**
3. **最小可用不要求改 Hermes 本体**
4. **如果想得到更完整的 setup/status/web UI 集成，再额外应用 Hermes 集成增强补丁**

这样别人最容易理解，也不会误以为：

- “装了这个仓库就等于 Hermes 官方支持 QQ 个人号了”

---

## 12. 推荐验收顺序

安装好之后建议按这个顺序测试：

1. `hermes napcat-qq-bridge --help`
2. `hermes napcat-qq-bridge run`
3. `curl http://127.0.0.1:8096/healthz`
4. QQ 私聊发一句话
5. QQ 群里发一句话
6. 发一张图片
7. 发一个在线文件
8. 发：
   - `/reasoning`
   - `/reasoning high`
   - `/model`
   - `/model gpt-5.5`

---

## 13. 测试

仓库内有基础测试：

```bash
python -m pytest -q tests/test_napcat_qq_bridge.py
```

如果你本机没有 pytest，也可以直接用 Hermes 自己的 venv：

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest -q tests/test_napcat_qq_bridge.py
```

---

## 14. 许可证

MIT，见 `LICENSE`。
>>>>>>> 687a00e (Make the QQ personal bridge repo directly shareable on modern Hermes)
