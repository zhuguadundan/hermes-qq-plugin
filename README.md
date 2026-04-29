# Hermes QQ 插件 — 基于 NapCat / OneBot 的原生个人 QQ 消息平台适配器

这个仓库提供一个 **Hermes Gateway 原生平台适配器**，用于通过
[NapCat](https://napneko.github.io/) / OneBot 11 接入个人 QQ。

它不是旧版 `HermesCLI` 桥接器。旧桥接器会把 QQ 每轮消息包装成独立
CLI 调用，容易导致会话、模型切换、命令处理和文件发送行为与 Telegram、
Discord、微信等原生平台不一致。本插件改为把 OneBot 事件转换成 Hermes
原生 `MessageEvent`，让 QQ 共享 Hermes Gateway 的会话、命令、模型切换、
工具集、媒体发送、hooks、记忆和 transcript 逻辑。

## 为什么要改成原生平台适配器

旧的独立 QQ bridge 容易出现这些问题：

- 在配置文件或 WebUI 切换模型后，QQ 会话不一定跟着切换；
- `/new`、`/model`、`/reasoning`、`/stop`、`/reset` 等命令行为和原生平台不一致；
- 图片和紧随其后的文字容易被拆成两个独立 turn；
- 运行状态、工具名、本地路径等内部信息可能被发到 QQ 群里；
- 文件/图片/语音发送逻辑和 Hermes 正常 delivery 逻辑重复。

本插件只负责 QQ/NapCat/OneBot 传输层，真正的 AI 会话和业务逻辑仍由
Hermes Gateway 统一处理。

## 当前支持情况

- 支持个人 QQ（NapCat / OneBot 11）；
- 支持私聊；
- 支持群聊、群白名单、群内全消息响应；
- 支持接收文本、图片、语音/音频、视频、文件/文档；
- 支持发送文本、图片、语音/音频、视频、文件/文档；
- 支持 Hermes Gateway 原生命令：`/new`、`/model`、`/reasoning`、`/stop`、`/status` 等；
- 不支持官方 QQ Bot API；官方 QQ Bot 请继续使用 Hermes 的 `qqbot` 平台。

## 仓库结构

```text
hermes_qq/
  adapter.py      # Hermes BasePlatformAdapter 实现
  client.py       # NapCat/OneBot HTTP RPC 客户端
  types.py        # 共享数据类型

gateway_platform_shim/
  qq.py           # 安装到 Hermes gateway/platforms/qq.py 的 shim

scripts/
  install-native-qq.sh      # 安装包、复制 shim、修补 Hermes 本体
  patch_hermes_core.py      # 幂等的 Hermes 本体 patch 脚本

examples/
  config.qq.yaml            # Hermes config.yaml 配置示例

docs/
  architecture.md           # 架构说明
  hermes-core-changes.md    # 对 Hermes 本体的改动说明
```

## 前置条件

1. 已安装 Hermes，本地通常是：

   ```bash
   ~/.hermes/hermes-agent
   ```

2. Hermes 虚拟环境存在：

   ```bash
   ~/.hermes/hermes-agent/venv/bin/python
   ```

3. NapCat 已运行，并启用 OneBot HTTP 与 WebSocket。

   常见地址：

   ```text
   HTTP:      http://127.0.0.1:3000
   WebSocket: ws://127.0.0.1:3001
   ```

4. 如果 NapCat 配置了 access token，需要在 Hermes 配置中填写同一个 token。

## 安装

克隆本仓库：

```bash
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
```

安装到 Hermes：

```bash
./scripts/install-native-qq.sh ~/.hermes/hermes-agent
```

安装脚本会做三件事：

1. 用 Hermes 的 venv 执行 `pip install -e .`；
2. 把 `gateway_platform_shim/qq.py` 复制到 Hermes：

   ```text
   ~/.hermes/hermes-agent/gateway/platforms/qq.py
   ```

3. 执行 `scripts/patch_hermes_core.py`，给 Hermes 本体增加加载 `platforms.qq` 所需的最小改动。

安装后重启 Hermes Gateway：

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
```

如果你不是用 systemd 启动 Hermes，则重启你的 Gateway 进程，例如：

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -m hermes_cli.main gateway run --replace
```

## 配置 Hermes

把 `examples/config.qq.yaml` 合并到：

```text
~/.hermes/config.yaml
```

### 最小私聊配置

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
        - '123456789'   # 允许私聊的 QQ 号

      group_policy: disabled

  # 如果只使用个人 QQ / NapCat，建议显式关闭官方 QQBot
  qqbot:
    enabled: false
```

### 群聊配置

```yaml
platforms:
  qq:
    enabled: true
    extra:
      onebot_url: http://127.0.0.1:3000
      onebot_token: YOUR_NAPCAT_TOKEN
      onebot_ws_url: ws://127.0.0.1:3001
      onebot_ws_token: YOUR_NAPCAT_TOKEN

      group_policy: allowlist
      group_allow_from:
        - '987654321'   # 允许的 QQ 群号

      # true 表示允许的群里所有消息都会交给 Hermes；
      # false 表示通常需要 @机器人 才响应。
      group_chat_all: true

      # false 表示同一个群共享一个 Hermes 会话；
      # true 表示按群内用户拆分会话。
      group_sessions_per_user: false

    home_channel:
      platform: qq
      chat_id: group:987654321
      name: QQ 群 987654321

  qqbot:
    enabled: false
```

### 推荐的 QQ 显示配置

QQ 不能像 Telegram / Discord 那样编辑已发送消息，所以中途进度和流式输出会变成永久消息。
建议这样配置，减少群聊噪音：

```yaml
display:
  platforms:
    qq:
      tool_progress: off
      streaming: false
      interim_assistant_messages: false
      show_reasoning: false
```

这不会禁用必要的用户可见回复。`/new`、`/model`、`/reasoning`、错误提示、最终回复等仍会正常发送。

## NapCat / OneBot 检查清单

在 NapCat 中确认：

- 已启用 OneBot HTTP；
- 已启用 OneBot WebSocket；
- Hermes 中配置的 token 与 NapCat token 一致；
- 如果要接收图片/文件，NapCat 能返回可访问的 URL 或文件信息；
- 如果要发送文件，NapCat 支持相关文件上传/发送 API。

可以用下面命令测试 NapCat HTTP：

```bash
curl -H 'Authorization: Bearer YOUR_NAPCAT_TOKEN' \
  http://127.0.0.1:3000/get_login_info
```

如果没有配置 token，可以去掉 header。

## 可用命令

安装为原生平台后，QQ 直接使用 Hermes Gateway 命令：

```text
/new                 重置当前 QQ 会话
/reset               /new 的别名
/model               查看当前模型
/model gpt-5.5       切换当前会话模型
/reasoning           查看思考强度与思考显示状态
/reasoning high      设置思考强度
/reasoning hide      关闭思考内容显示
/stop                停止当前运行
/status              查看状态
/help                查看帮助
```

由于 QQ 现在是原生 Gateway 平台，通过 Hermes 配置文件或 WebUI 做的模型切换也应该作用于 QQ 会话。

## 对 Hermes 本体做了哪些改动

Hermes 目前还没有稳定的第三方平台适配器注册机制，所以本插件需要对 Hermes 本体做一层小 patch。
安装脚本会自动执行，详情见：

- [`docs/hermes-core-changes.md`](docs/hermes-core-changes.md)
- [`scripts/patch_hermes_core.py`](scripts/patch_hermes_core.py)

概要如下：

1. `gateway/config.py`
   - 增加 `Platform.QQ = "qq"`；
   - 识别 `platforms.qq.extra.onebot_url` / `onebot_ws_url`；
   - 把个人 QQ/NapCat 和官方 `qqbot` 区分开。

2. `gateway/run.py`
   - 在 `GatewayRunner._create_adapter()` 中加载 `NapCatQQAdapter`；
   - 增加 QQ allowlist 环境变量映射：
     - `QQ_ALLOWED_USERS`
     - `QQ_GROUP_ALLOWED_USERS`
     - `QQ_ALLOW_ALL_USERS`

3. `gateway/platforms/qq.py`
   - 安装 shim，从外部包 `hermes_qq` 加载适配器。

4. `gateway/platforms/__init__.py`
   - 导出 `NapCatQQAdapter`。

5. `hermes_cli/platforms.py`
   - 注册 `qq` 平台，显示为 `QQ Personal`；
   - 默认 toolset 为 `hermes-qq`。

6. `toolsets.py`
   - 增加 `hermes-qq`；
   - 将其加入 `hermes-gateway` 聚合 toolset。

7. `gateway/display_config.py`
   - QQ 采用不可编辑消息平台的低噪音默认显示策略。

长期更优雅的方式是 Hermes 本体提供正式的第三方平台 adapter entrypoint，例如 Python entry points：

```text
hermes.gateway_platforms
```

到那时，本仓库就不再需要 patch Hermes core，只需要安装 Python 包即可。

## 故障排查

### Gateway 没有加载 QQ

检查：

```bash
grep -n "QQ =" ~/.hermes/hermes-agent/gateway/config.py
ls ~/.hermes/hermes-agent/gateway/platforms/qq.py
~/.hermes/hermes-agent/venv/bin/python -c 'import hermes_qq; print(hermes_qq.NapCatQQAdapter)'
```

然后重启：

```bash
systemctl --user restart hermes-gateway.service
```

### QQ 消息没有触发回复

重点检查：

- 私聊白名单：`platforms.qq.extra.allow_from`
- 群白名单：`platforms.qq.extra.group_allow_from`
- 群里是否设置了 `group_chat_all: true`
- NapCat WebSocket 是否连接成功
- Hermes 日志：

  ```text
  ~/.hermes/logs/agent.log
  ~/.hermes/logs/errors.log
  ```

### 文件发不出去

文件发送依赖 NapCat 的文件 API。请确认：

- 机器人 QQ 账号本身能在该私聊/群发送文件；
- NapCat 支持并开放 `upload_file_stream`；
- 群文件可能需要 `upload_group_file`；
- 私聊在线文件可能需要 `send_online_file`；
- Hermes 进程能读取要发送的本地文件路径。

### QQ 群里出现过多运行状态

设置：

```yaml
display:
  platforms:
    qq:
      tool_progress: off
      streaming: false
      interim_assistant_messages: false
```

QQ 不能编辑消息，这些中途状态如果打开会变成永久消息。

## 开发与测试

运行插件仓库测试：

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest -q
```

如果你同时在 Hermes 本体里开发，建议额外运行 Hermes 的 QQ/Gateway 相关测试。

## 许可证

MIT
