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
- `group_ignore_from`: 群消息发送者 QQ 号过滤列表，默认 `[]`。按 QQ 号匹配，优先于群成员白名单；过滤发送者的普通消息、@ 消息、命令和文件上传通知均不进入消息处理或上下文，不占用最近 N 条的名额。实时消息和历史回补共用此规则，仅影响群聊。群文件列表扫描仍保留文件归档，但不会将过滤发送者的上传记录自动注入上下文。已有会话、已归档文件以及其他成员主动引用/转述的文字不作删除。
- `group_chat_all`: `true` 时处理允许群内的普通消息；`false` 时只处理 @ 机器人的消息
- `group_listen_context`: 只响应 @ 时保留多少条未 @ 的群消息作为上下文（`0` 关闭）

例如，在 `platforms.qq.extra` 中配置（请替换成要过滤的 QQ 号）：

```yaml
group_ignore_from:
  - '123456789'
```

修改配置后重启 Gateway，旧的内存消息缓冲随之清空。

### 只回 @ 但听得见群聊

`group_chat_all: false` 会让非 @ 消息根本进不了 Hermes，模型完全不知道中间聊了什么。
`group_listen_context: N` 补上这块：未 @ 的文字消息只写进内存环形缓冲，**不触发模型、不回复**；图片和语音只记占位符，群文件按下面的归档规则后台下载；
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
- 上下文消息有 TTL（`group_listen_context_ttl_minutes`，默认 120 分钟）：群安静两小时后被 @，不会把两小时前的闲聊塞进去；过期条目在追加和注入时清理，健康检查每小时也扫一遍
- `/new`、`/model` 等命令不拼上下文，保持命令语义；历史回补从不重新执行命令
- 即使聊天缓冲已清空或过期，只要本群归档目录存在，每次普通 @ 请求仍提供目录入口
- @ 遇到尚在下载的相关文件，最多等 `group_file_wait_seconds`（默认 5 秒）；超时标明“正在下载”，下载继续，WebSocket 仍可接收新消息

### 群文件：被动收集

QQ 群里**没法 @ 一个人再附一个文件**——发文件就是单独一条文件消息，不会带@。
所以只回 @ 的模式下，文件原本永远进不了 Hermes。打开 `group_file_autosave` 后：

- 有人往群文件上传 → `group_upload` 通知到达 → 后台异步下载到本地，**不回复、不触发模型**
- 被 @ 时上下文里写的是带本地路径的条目，模型可以直接读：

```text
10:32 张三: [文件] 周报.pdf → 已存到本地: ~/.hermes/cache/qq_group_files/1054660428/20261028-103200-周报.pdf
```

- `group_file_backfill` 显式请求最多 10,000 个文件并遍历子目录，避免接口默认只返回前 50 个。每轮最多下载 `group_file_backfill_limit` 个新文件、总计 1GiB；未收完时约一分钟后续扫，平时每小时复查，不需要靠反复重启推进
- 只有落在 `history_backfill_seconds` 窗口内的文件才会进上下文缓冲；更早的文件只在盘上，上下文头部会给出该群的目录路径，模型自己去 `ls` 就能找到
- 落盘位置：`~/.hermes/cache/qq_group_files/<群号>/`，索引在 `index.json`。按群、目录、文件名、大小、上传者和版本时间判断，兼容旧索引；下载后记录 SHA-256。同名同大小的新版本不会复用旧内容，文件与临时文件使用唯一名称，中文名按字节长度裁剪
- 超过 `group_file_max_mb` 的文件不下载，上下文里标一句「超过本地保存上限」
- `group_file_retention_days` 到了自动清理（文件操作时 + 健康检查每小时扫一次，不用等重启）
- `group_file_cache_max_gb` 是**后置兜底**：先按留存天数清，仍超限才按最旧优先淘汰。七天是最长保留窗口，超过容量时可以提前淘汰；两种清理都保留墓碑，回扫不会立即重新下载同一版本
- `group_file_concurrency` 默认 2；下载中预留字节也计入群文件容量，下载完成后再次执行到期清理和容量检查。流式读取、大小限制、总时限和退出停止标志适用于远程媒体下载，避免把整个大文件一次读入内存
- 5GiB 限制针对群文件归档目录；私聊和普通媒体使用 Hermes 自有缓存及其清理策略，不把其他平台缓存静默纳入群文件淘汰
- 断开时停止下载、扫描、接收及待处理批次，并等待后台任务回收

```yaml
platforms:
  qq:
    extra:
      group_listen_context: 15
      group_file_autosave: true
      group_file_backfill: true
      group_file_backfill_limit: 50
      group_file_max_mb: 200
      group_file_retention_days: 7
      group_file_cache_max_gb: 5
      group_file_concurrency: 2
      group_file_wait_seconds: 5
      group_listen_context_ttl_minutes: 120
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

历史回补只主动查询明确配置的私聊和群聊白名单目标。窗口外消息忽略，管理命令永不回放。成功分派的消息 ID 写入 `cache/qq_state/events-<机器人QQ号>.sqlite3`，保留七天，重启后仍去重；解析失败不记成功，可重投。首次启用持久化时，启用前的旧群消息只补上下文，不自动重跑旧请求。接收使用有界队列，历史回补和实时分派保持先后顺序，HTTP 拉历史期间不阻塞 WebSocket 收帧。

持久化记录表示“已交给 Hermes 处理器”，不是模型任务执行成功的证明。进程在分派与落盘之间硬中断时，仍存在极窄的不确定窗口；不声称任意工具副作用都能严格执行一次。明确设为 `0` 的回补数量或时间会关闭回补，批次延迟 `0` 会立即分派；负数、非有限数值等无效配置会明确报错。

HTTP 路由先通过只读请求探测；发送和上传超时不再换另一条路由盲目重试，并检查 OneBot `retcode`。结果未知时记录错误，避免把可能已经发送的操作直接再发一次。

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
