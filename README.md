# Hermes QQ Native Adapter

这是一个通过 NapCat / OneBot 11 将个人 QQ 接入 Hermes Gateway 的原生平台适配器。

本仓库同时作为当前可用本地版本的远程备份。代码、安装脚本、配置示例、测试和恢复说明均保存在仓库中；如果本地目录被误删，可以按照本文的“从 GitHub 恢复”章节重新安装。

> 适用于个人 QQ + NapCat，不是腾讯官方 QQ Bot API。官方机器人应继续使用 Hermes 的 `qqbot` 平台。

## 当前能力

- 私聊和群聊收发文本消息
- 接收和发送图片、语音、视频、文件
- 私聊白名单、群白名单、群成员白名单
- 可选择响应群内全部消息或只响应 @ 机器人的消息
- 只响应 @ 时仍可保留群聊上下文（`group_listen_context`）：未 @ 的消息只进内存缓冲，不触发模型，被 @ 时作为只读上下文拼接
- 被动收下群文件（`group_file_autosave`）：QQ 群里无法 @ + 附文件，所以收到上传就后台存本地，被 @ 时把本地路径一起给模型
- 图片和紧随其后的文字可合并为一个 Hermes turn
- OneBot WebSocket ping/pong、断线重连和 NapCat 健康检查
- 重连后按白名单目标回补最近一段时间内的消息
- 对重复的 WebSocket/历史消息进行去重
- 使用 Hermes 原生会话、命令、模型切换、工具、记忆和 delivery 流程
- 兼容不同 `websockets` 版本的请求头参数

## 工作方式

```text
QQ
  -> NapCat / OneBot 11
  -> hermes_qq.NapCatQQAdapter
  -> Hermes MessageEvent
  -> Hermes Gateway 会话、命令、模型、工具和消息发送
```

这个插件只负责 QQ 传输层。它不会为每条 QQ 消息启动独立 CLI，也不会维护一套与 Hermes 分离的 AI 会话。

## 前置条件

- Python 3.11 或更高版本
- 已安装 Hermes，默认路径为 `~/.hermes/hermes-agent`
- Hermes venv 位于 `~/.hermes/hermes-agent/venv`
- NapCat 已登录 QQ
- NapCat 已启用 OneBot 11 HTTP 和正向 WebSocket

默认端点：

```text
HTTP:      http://127.0.0.1:3000
WebSocket: ws://127.0.0.1:3001
```

## 首次安装

```bash
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git ~/hermes-qq-plugin
cd ~/hermes-qq-plugin
./scripts/install-native-qq.sh ~/.hermes/hermes-agent
```

安装脚本会：

1. 在 Hermes venv 中执行 editable 安装；
2. 安装 `gateway/platforms/qq.py` shim；
3. 尝试以幂等方式补齐 Hermes 加载个人 QQ 平台所需的 core 改动；
4. 编译检查安装后的 shim。

然后把 `examples/config.qq.yaml` 中需要的配置合并到 `~/.hermes/config.yaml`，并重启 Gateway：

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
```

确认实际加载的是本地仓库：

```bash
~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import hermes_qq
print(hermes_qq.__file__)
PY
```

输出应位于 `~/hermes-qq-plugin/hermes_qq/`。

## 从 GitHub 恢复

如果 `~/hermes-qq-plugin` 被误删，不要先创建同名空目录，直接重新克隆并安装：

```bash
cd ~
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
./scripts/install-native-qq.sh ~/.hermes/hermes-agent
systemctl --user restart hermes-gateway.service
```

验证恢复结果：

```bash
cd ~/hermes-qq-plugin
~/.hermes/hermes-agent/venv/bin/python -m pytest -q
~/.hermes/hermes-agent/venv/bin/python -c 'import hermes_qq; print(hermes_qq.__file__)'
systemctl --user is-active hermes-gateway.service
```

如果 editable 安装仍指向已经删除的旧路径，重新运行安装脚本即可修复。更完整的恢复、检查和故障排查步骤见 [`docs/recovery.md`](docs/recovery.md)。

### 远程仓库没有备份的内容

这个仓库不会保存以下本机数据：

- `~/.hermes/config.yaml` 中的真实 token、QQ 号和群号
- Hermes 会话、记忆、日志、cron 和缓存
- NapCat 登录状态、设备信息和配置
- Hermes 主仓库中的其他未提交自定义修改
- systemd service 中的本机环境变量

因此请另外备份 `~/.hermes/config.yaml` 和重要的 Hermes 数据。不要把真实 token 提交到本仓库。

## 配置

推荐从 [`examples/config.qq.yaml`](examples/config.qq.yaml) 复制所需字段。最小私聊配置：

```yaml
platforms:
  qq:
    enabled: true
    extra:
      onebot_url: http://127.0.0.1:3000
      onebot_token: YOUR_NAPCAT_TOKEN
      onebot_ws_url: ws://127.0.0.1:3001
      onebot_ws_token: YOUR_NAPCAT_TOKEN

      dm_policy: allowlist
      allow_from:
        - '123456789'

      group_policy: disabled

  qqbot:
    enabled: false
```

群聊配置示例：

```yaml
platforms:
  qq:
    enabled: true
    extra:
      group_policy: allowlist
      group_allow_from:
        - '987654321'
      group_chat_all: true
      group_sessions_per_user: false
```

### 访问策略

- `dm_policy`: `open`、`allowlist` 或 `disabled`
- `group_policy`: `open`、`allowlist` 或 `disabled`
- `allow_from`: 允许私聊的 QQ 号
- `group_allow_from`: 允许接入的群号
- `allowed_group_users`: 可选的群成员 QQ 号白名单
- `group_chat_all`: `true` 时处理允许群内的普通消息；`false` 时只处理 @ 机器人的消息
- `group_listen_context`: 只响应 @ 时保留多少条未 @ 的群消息作为上下文（`0` 关闭）

### 只回 @ 但听得见群聊

`group_chat_all: false` 会让非 @ 消息根本进不了 Hermes，模型完全不知道中间聊了什么。
`group_listen_context: N` 补上这块：未 @ 的消息只写进内存环形缓冲，**不触发模型、不回复、不下载媒体**；
等到真的被 @ 时，最近 N 条会作为只读上下文拼在那一次请求前面，之后清空缓冲（避免重复带入）。

```yaml
platforms:
  qq:
    extra:
      group_chat_all: false
      group_listen_context: 15
```

注入形态：

```text
[群聊上下文，共 2 条，仅供参考，不要逐条回复，也不要声称自己参与了这些发言]
10:31 张三: 今天开会吗
10:32 李四: 开，三点
[当前消息] 你在吗
```

细节：

- 缓冲按群号隔离，`maxlen` 就是 `group_listen_context`
- 图片/语音/视频只记占位符（`[图片]`、`[语音]`），不下载，控制缓存和隐私
- 单行截断 200 字符，整体注入上限 2500 字符
- `/new`、`/model` 等命令不拼上下文，保持命令语义

### 群文件：被动收集

QQ 群里**没法 @ 一个人再附一个文件**——发文件就是单独一条文件消息，不会带@。
所以只回 @ 的模式下，文件原本永远进不了 Hermes。打开 `group_file_autosave` 后：

- 有人往群文件上传 → `group_upload` 通知到达 → 后台异步下载到本地，**不回复、不触发模型**
- 被 @ 时上下文里写的是带本地路径的条目，模型可以直接读：

```text
10:32 张三: [文件] 周报.pdf → 已存到本地: ~/.hermes/cache/qq_group_files/1054660428/20261028-103200-周报.pdf
```

- 重启期间漏掉的 `group_upload` 由 `group_file_backfill` 补：连上就扫一遍群文件列表，只取最近 `group_file_backfill_limit` 个未收录的文件
- 落盘位置：`~/.hermes/cache/qq_group_files/<群号>/`，索引在 `index.json`（按 `群号:file_id` 去重，重启不重复下载）
- 超过 `group_file_max_mb` 的文件不下载，上下文里标一句「超过本地保存上限」
- `group_file_retention_days` 到了自动清理（启动后首次文件操作时扫一次）

```yaml
platforms:
  qq:
    extra:
      group_listen_context: 15
      group_file_autosave: true
      group_file_backfill: true
      group_file_backfill_limit: 10
      group_file_max_mb: 30
      group_file_retention_days: 30
```

生产或个人部署建议使用 `allowlist`，不要在公网暴露无鉴权的 OneBot HTTP 端口。

### 重连和历史回补

```yaml
platforms:
  qq:
    extra:
      history_backfill_count: 20
      history_backfill_seconds: 600
      ws_reconnect_delay_seconds: 3
      ws_ping_interval_seconds: 25
      ws_ping_timeout_seconds: 15
      health_check_interval_seconds: 15
```

历史回补只会主动查询明确配置的私聊和群聊白名单目标。窗口外的消息会被忽略，已处理消息会按消息标识去重。

### QQ 显示建议

QQ 不支持编辑已发送消息。为避免工具进度和流式中间结果永久留在聊天中，建议：

```yaml
display:
  platforms:
    qq:
      tool_progress: off
      streaming: false
      interim_assistant_messages: false
      show_reasoning: false
```

## 常用 Hermes 命令

QQ 使用 Gateway 原生命令，例如：

```text
/new
/reset
/model
/model <模型名>
/reasoning
/reasoning high
/reasoning hide
/stop
/status
/help
```

## 检查 NapCat

无 token：

```bash
curl http://127.0.0.1:3000/get_login_info
curl http://127.0.0.1:3000/get_status
```

有 token：

```bash
curl -H 'Authorization: Bearer YOUR_NAPCAT_TOKEN' \
  http://127.0.0.1:3000/get_status
```

正常情况下 `get_status` 数据中应包含 `online: true` 和 `good: true`。

## 开发和测试

```bash
cd ~/hermes-qq-plugin
~/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_qq/*.py
~/.hermes/hermes-agent/venv/bin/python -m pytest -q
git diff --check
```

如果同时修改了 Hermes 主仓库，再运行 Hermes 的 QQ 集成测试：

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -m pytest -q tests/gateway/test_native_qq_adapter.py
```

## 仓库结构

```text
hermes_qq/                  插件实现
  adapter.py                Hermes 平台适配器
  client.py                 NapCat/OneBot HTTP 客户端
  types.py                  QQ 事件源类型

gateway_platform_shim/qq.py
                            Hermes 导入 shim
scripts/install-native-qq.sh
                            安装/恢复入口
scripts/patch_hermes_core.py
                            Hermes core 幂等补丁脚本
examples/config.qq.yaml      无真实凭据的配置模板
tests/                       插件回归测试
docs/architecture.md         运行架构
docs/hermes-core-changes.md  Hermes core 集成说明
docs/recovery.md             灾难恢复清单
```

## 升级注意事项

Hermes 目前没有稳定的第三方平台 adapter 注册接口，所以安装脚本需要按源码标记修改 Hermes core。Hermes 升级后这些标记可能变化；如果安装日志出现 `warn: marker not found`，不要忽略，应参照 [`docs/hermes-core-changes.md`](docs/hermes-core-changes.md) 手动核对。

## License

MIT
