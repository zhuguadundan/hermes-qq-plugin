# Hermes QQ Plugin

NapCat + Hermes 的 QQ 桥接插件仓库。

当前主方案是：

- **入站：OneBot WebSocket Server（纯 WS）**
- **出站：OneBot HTTP API**

这样做的目的很明确：

- 避免 HTTP webhook 单次投递丢消息
- 避免 HTTP / WS 双路重复投递
- 在 WS 断线重连后做一次历史补拉，提升真实场景稳定性

---

## 目录结构

```text
.
├── napcat_qq_bridge/
│   ├── __init__.py
│   ├── bridge.py
│   ├── cli.py
│   ├── config.example.json
│   ├── plugin.yaml
│   └── README.md
├── tests/
├── examples/
│   ├── docker-compose.napcat.yml
│   └── systemd/hermes-napcat-qq-bridge.service
└── scripts/install-plugin.sh
```

---

## 功能

- QQ 私聊 / 群聊接入 Hermes
- 每个聊天独立 session，自动 `--resume`
- 同一聊天串行处理；处理中收到 follow-up 会中断当前 Hermes 并合并消息
- 支持 `/new` `/reset` `/status` `/stop` `/help`
- 支持文本、图片、语音、视频、文件收发
- 群文件上传 notice 会进入会话
- 群聊默认只响应：
  - `@机器人`
  - 回复机器人上一条消息
- 纯 WS 入站，默认禁用 webhook 入站
- WS 重连后自动做一次 catch-up 补拉
- 过滤 Hermes CLI 噪音，减少脏输出和重复段落

---

## 环境要求

- Linux / WSL2
- 已安装并可正常运行的 Hermes Agent
- 已登录的 NapCat
- Python 依赖：
  - `requests`
  - `websockets`（Hermes 自带 venv 已包含）

---

## 安装

```bash
git clone git@github.com:zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
bash scripts/install-plugin.sh
```

安装后插件会复制到：

```text
~/.hermes/plugins/napcat_qq_bridge
```

---

## 配置

先复制示例配置：

```bash
mkdir -p ~/.hermes/napcat_qq_bridge
cp napcat_qq_bridge/config.example.json ~/.hermes/napcat_qq_bridge/config.json
$EDITOR ~/.hermes/napcat_qq_bridge/config.json
```

核心配置：

- `onebot.url`
  - NapCat HTTP API 地址
- `onebot.token`
  - NapCat HTTP API token
- `onebot.ws_url`
  - NapCat OneBot WebSocket Server 地址
- `onebot.ws_token`
  - NapCat WS token
- `bridge.receive_mode`
  - 现在默认就是 `ws`

详细字段说明见：

- `napcat_qq_bridge/README.md`

---

## NapCat 推荐配置

### 1. HTTP Server

用于桥向 QQ 发消息：

- Host: `127.0.0.1`
- Port: `3000`
- Token: 你的 token
- `messagePostFormat: array`

### 2. WebSocket Server

用于 NapCat 向桥推送消息：

- Host: `0.0.0.0` 或 `127.0.0.1`
- Port: `3001`
- Access Token: 你的 token

### 3. HTTP Client

**建议关闭。**

纯 WS 模式下，桥会拒绝 `/napcat` webhook 入站，避免双路重复投递。

---

## 启动

```bash
hermes napcat-qq-bridge run --config-file ~/.hermes/napcat_qq_bridge/config.json
```

systemd 例子见：

```text
examples/systemd/hermes-napcat-qq-bridge.service
```

---

## 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

纯 WS 正常时，你应该看到这些关键信息：

- `"receive_mode": "ws"`
- `"webhook_ingress_enabled": false`
- `"websocket_connected": true`

---

## 开发与测试

```bash
cd hermes-qq-plugin
python -m pytest -q
```

---

## 代码审查结论（本次）

我对插件做了一轮全面 review，主要结论是：

### 已修复/已落地

1. **仓库代码与线上运行版曾经不同步**
   - 现在已经把纯 WS + 健壮性改造同步回仓库

2. **README 明显落后于实际实现**
   - 之前仍以 HTTP webhook 为主
   - 现在已更新为纯 WS 方案

3. **缺少 WS 相关测试覆盖**
   - 现在补了：
     - WS 配置解析
     - WS 启动预检
     - WS 场景下缺口恢复
     - 新默认值校验

4. **本地运行产物不该进入仓库**
   - 已把：
     - `napcat_qq_bridge/config.json`
     - `napcat_qq_bridge/bridge.runtime.log`
   - 加入忽略规则

### 当前保留的设计取舍

- 入站用 WS，出站仍保留 HTTP API
  - 这是刻意保守的设计
  - 因为发送链路已经稳定，没必要同时重构两端

---

## License

MIT
