# Hermes QQ Plugin

一个给 Hermes Agent 用的 QQ 桥接插件。  
基于 NapCat OneBot HTTP，把 QQ 私聊/群聊消息转给本机 Hermes，再把文本、图片、语音、视频、文件回发到 QQ。

## 功能

- 私聊 / 群聊接入 Hermes
- 每个聊天独立 session，自动 `--resume`
- 同一聊天串行处理，follow-up 会中断并合并
- 支持 `/new` `/reset` `/status` `/stop` `/help`
- 支持图片、语音、视频、文件收发
- webhook 正常时优先用 webhook
- webhook 丢事件时，自动用历史轮询兜底
- 过滤 Hermes CLI 噪音，减少“重复回复”

## 仓库结构

```text
.
├── napcat_qq_bridge/
│   ├── __init__.py
│   ├── bridge.py
│   ├── cli.py
│   └── plugin.yaml
├── tests/
├── scripts/install-plugin.sh
└── examples/
```

## 环境要求

- Linux / WSL2
- 已安装并可正常对话的 Hermes Agent
- 已登录的 NapCat
- Python `requests`

## 安装

```bash
git clone git@github.com:zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
bash scripts/install-plugin.sh
hermes napcat-qq-bridge --help
```

安装后插件会被复制到：

```text
~/.hermes/plugins/napcat_qq_bridge
```

## NapCat 配置

推荐 OneBot11 这样配：

### HTTP Server

- Host: `127.0.0.1`
- Port: `3000`
- Token: 你的 OneBot token
- `messagePostFormat`: `array`

### HTTP Client

- URL: `http://127.0.0.1:8096/napcat`
- `messagePostFormat`: `array`

## 启动示例

```bash
hermes napcat-qq-bridge run \
  --onebot-url http://127.0.0.1:3000 \
  --onebot-token YOUR_ONEBOT_TOKEN \
  --listen-host 127.0.0.1 \
  --listen-port 8096 \
  --webhook-path /napcat \
  --allow-user 123456789 \
  --allow-group 987654321 \
  --hermes-workdir /path/to/workdir \
  --hermes-toolsets terminal,file,web \
  -v
```

默认会保留 **3 秒历史轮询兜底**。  
如果现场 webhook 不稳定，插件仍然能继续工作。

## systemd

可参考：

```text
examples/systemd/hermes-napcat-qq-bridge.service
```

## 群聊触发规则

默认群里只在下面两种情况回复：

- `@机器人`
- 回复机器人上一条消息

如果你想群里所有消息都处理，启动时加：

```bash
--group-chat-all
```

## Hermes 输出媒体

如果 Hermes 要回图片、语音、视频或文件，在最终回复里输出：

```text
MEDIA:/绝对路径
```

如果音频要按 QQ 语音而不是普通文件发送，再额外带上：

```text
[[audio_as_voice]]
```

## 聊天命令

- `/new` / `/reset`：重置当前聊天 session
- `/status`：查看当前聊天状态
- `/stop`：停止当前处理
- `/help`：查看帮助

## 健康检查

```bash
curl http://127.0.0.1:8096/healthz
```

## 常见问题

### 1. QQ 发消息没回复

先查三件事：

1. NapCat HTTP Server 是否真的能访问 `127.0.0.1:3000`
2. HTTP Client 是否真的上报到 `http://127.0.0.1:8096/napcat`
3. 插件健康检查是否正常

### 2. 群里不回复

通常是因为没有 `@机器人`，也不是回复机器人消息。  
可临时加 `--group-chat-all` 排查。

### 3. 文件 / 语音偶发失败

这部分有时是 NapCat 上游接口本身不稳定。  
当前插件已经做了更多 fallback，但如果 NapCat 没返回可下载资源，桥也无法凭空恢复。

## 开发与测试

```bash
/home/dawei/.hermes/hermes-agent/venv/bin/python -m pytest -q
```

## 当前状态

这份仓库已经包含以下修复：

- 无回复回归修复（恢复轮询兜底）
- 重复回复去重增强
- 私聊在线文件收发支持
- 图片 / 语音 / 文件解析路径修正

如果你只是想直接用，按上面的安装和启动示例配置即可。
