# Hermes Patch Checklist for This QQ Plugin

重要说明：

这个 QQ 插件不是 Hermes 官方内置支持的平台。

它是给小范围自用 / 朋友间使用的 NapCat + Hermes 方案，所以：
- 插件仓库只提供桥接插件本身
- 但如果你想要“像原生消息渠道一样稳定”，通常还需要给本机 Hermes 打补丁

也就是说：
- 这个仓库 != 官方 Hermes 发布物
- 直接把插件复制到 `~/.hermes/plugins/napcat_qq_bridge` 并不保证“cron、权限、工具路由、QQ 会话、危险命令审批”全部工作
- 你还需要确认 Hermes 本体已经包含下面这些修复

---

## 1. 为什么需要给 Hermes 本体打补丁

这个插件仓库解决的是：
- NapCat / OneBot 到 Hermes 的 QQ 桥接
- 私聊 / 群聊会话复用
- QQ 媒体、命令、中断、结构化结果清洗

但 Hermes 本体原来并不认识一个叫 `qq` 的正式平台。

所以如果你在 Hermes 里想同时获得这些能力：
- 原生 `qq` 平台
- cron 任务投递到 `qq:group:...`
- `send_message` 能发到 `qq`
- 平台默认 toolset 不报 `KeyError: 'qq'`
- Gateway 授权层读取 `platforms.qq.extra` 的 allowlist
- QQ 渠道默认不再弹危险命令审批

那 Hermes 本体必须同步补齐。

---

## 2. 必要补丁清单（Hermes 本体）

下面这些修复不是插件仓库内的文件，而是你本机 Hermes 仓库里的改动。

### 2.1 新增原生平台 `qq`

文件：
- `gateway/config.py`
- `gateway/run.py`
- `gateway/platforms/qq.py`

目的：
- 让 Hermes Gateway 能真正创建和运行 `Platform.QQ`
- 区分：
  - `qq` = NapCat / OneBot 个人 QQ
  - `qqbot` = 官方 QQ Bot API v2

### 2.2 补齐平台默认 toolset 注册

文件：
- `hermes_cli/platforms.py`
- `toolsets.py`

目的：
- 解决 `KeyError: 'qq'`
- 让 Hermes 在运行 agent 时能为 `qq` 平台选到默认工具集

如果没这一步，会出现：
- 私聊 / 群聊收到消息
- agent 真正开始执行时崩掉
- 报错：`KeyError: 'qq'`

### 2.3 修复 cron 到 QQ 的投递映射

文件：
- `cron/scheduler.py`
- `tools/send_message_tool.py`

目的：
- 让 cron 能识别：
  - `deliver: qq:group:YOUR_GROUP_ID`
- 让 `send_message` 工具能识别：
  - `target: qq:group:YOUR_GROUP_ID`

如果没这一步，症状通常是：
- cron 任务执行成功
- output 文件也生成了
- 但最后投递失败
- `jobs.json` 里会出现：
  - `last_delivery_error: unknown platform 'qq'`

### 2.4 修复 Gateway 授权逻辑对 QQ 的支持

文件：
- `gateway/run.py`

目的：
- 让 `qq` 平台的授权判断读取 `platforms.qq.extra` 里的：
  - `allow_from`
  - `group_allow_from`
  - `allowed_group_users`
  - `allow_all`

如果没这一步，症状通常是：
- QQ 平台明明已经在 config 里配了 allowlist
- 但 Gateway 仍然把消息当成未授权用户
- 日志里会出现类似：
  - `Unauthorized user: ... on qq`

### 2.5 修复 QQ 原生 adapter 的出站回复样式

文件：
- `gateway/platforms/qq.py`

目的：
- 不要每次回复都引用用户原消息
- 文本 / 图片 / 语音 / 视频 / 文件统一走“普通发送”

如果没这一步，症状通常是：
- 群聊和私聊里每次都显示成“引用回复”

### 2.6 修复 cron 消息自动包装

文件：
- `~/.hermes/config.yaml`

配置项：

```yaml
cron:
  wrap_response: false
```

目的：
- 避免 cron 消息自动变成：
  - `Cronjob Response: ...`
  - `Note: The agent cannot see this message...`

如果没这一步，症状通常是：
- 你收到的 cron 消息不是纯正文
- 而是一大段系统包装文字

### 2.7 关闭 QQ 渠道的危险命令审批

文件：
- `~/.hermes/config.yaml`
- `gateway/run.py`

推荐做法有两层：

#### 方案 A：全局关闭审批（最直接）

```yaml
approvals:
  mode: off
```

作用：
- 所有危险命令审批都关闭
- QQ 渠道不会再弹：
  - `Dangerous command requires approval`

#### 方案 B：只对 QQ 渠道默认放行

在 `platforms.qq.extra` 里增加：

```yaml
platforms:
  qq:
    extra:
      auto_approve_dangerous_commands: true
```

并且 Hermes 本体的 `gateway/run.py` 需要支持这个字段。

作用：
- 只让 QQ 会话自动进入 YOLO/免审批
- 不影响其他渠道

如果没这一步，症状通常是：
- 用户在 QQ 里发图片后，agent 想执行图像处理脚本
- 结果被危险命令审批拦住
- QQ 收到一条 `/approve` / `/deny` 提示，而不是正常结果

### 2.8 统一 QQ 渠道的重启方式

文件：
- `~/.config/systemd/user/hermes-gateway.service`
- 以及本地运维说明

当前建议：
- 不再重启旧的独立 bridge 服务
- 只重启：

```bash
systemctl --user restart hermes-gateway.service
```

原因：
- 你现在实际在跑的是 Hermes Gateway 内部的原生 `qq` 平台
- 旧的 `hermes-napcat-qq-bridge.service` 只是历史遗留 / 回滚用途

如果没这一步，症状通常是：
- 改了 `config.yaml` 以后去重启错误的服务
- 结果 QQ 渠道配置根本没生效

---

## 3. 建议你如何判断 Hermes 是否已经打好补丁

### 3.1 看 Gateway 运行状态

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / '.hermes' / 'gateway_state.json'
state = json.loads(p.read_text())
print(state.get('gateway_state'))
print(state.get('platforms', {}).get('qq'))
PY
```

期望：
- `gateway_state = running`
- `platforms.qq.state = connected`

### 3.2 看 `qq` 默认 toolset 是否正常

```bash
source ~/.hermes/hermes-agent/venv/bin/activate
python - <<'PY'
from hermes_cli.tools_config import _get_platform_tools
print(sorted(_get_platform_tools({}, 'qq')))
PY
```

期望：
- 正常返回一组工具名
- 不报 `KeyError: 'qq'`

### 3.3 看 cron 投递是否正常认识 `qq`

如果你的 cron job 用的是：

```text
qq:group:YOUR_GROUP_ID
```

那它不应该再出现：

```text
unknown platform 'qq'
```

可以直接检查：

```bash
grep -R "unknown platform 'qq'" ~/.hermes/cron ~/.hermes/logs ~/.hermes/sessions 2>/dev/null
```

正常情况下应该没有新结果。

### 3.4 看 Gateway 有没有把 QQ 用户错判成未授权

```bash
journalctl --user -u hermes-gateway.service --since '30 min ago' --no-pager | grep 'Unauthorized user: .* on qq'
```

正常情况下，如果你已经配置了 allowlist，就不应该持续看到这类日志。

### 3.5 看 QQ 渠道是否还会弹危险命令审批

如果你已经配置：

```yaml
approvals:
  mode: off
```

或者：

```yaml
platforms:
  qq:
    extra:
      auto_approve_dangerous_commands: true
```

那么用户从 QQ 发图片、图像编辑请求等场景时，不应该再收到：

```text
Dangerous command requires approval
```

### 3.6 看重启命令是否统一

修改 `config.yaml` 后，请用：

```bash
systemctl --user restart hermes-gateway.service
```

而不是旧的独立 bridge 服务。

---

## 4. 推荐的最小 Hermes 修复集合

如果你不想研究所有细节，最少要确保 Hermes 本体已经包含这些内容：

1. `Platform.QQ` 存在
2. `gateway/platforms/qq.py` 存在并被 `gateway/run.py` 注册
3. `hermes_cli/platforms.py` 里有 `qq -> hermes-qq`
4. `toolsets.py` 里有 `hermes-qq`
5. `cron/scheduler.py` 能识别 `qq`
6. `tools/send_message_tool.py` 能识别 `qq`
7. `gateway/run.py` 的授权逻辑认识 `qq`
8. `cron.wrap_response = false`
9. QQ 渠道的危险命令审批已关闭（全局或按平台）
10. 你知道正确重启命令是 `systemctl --user restart hermes-gateway.service`

没有这些，插件本身可能装上了，但实际体验还是会不断 debug。

---

## 5. 当前维护建议

如果你是小范围和朋友一起用，建议这样维护：

### 方案 A：固定一份“已打补丁的 Hermes”

最稳。

做法：
- 统一使用同一个 Hermes 仓库版本
- 把上面列出来的补丁都打进去
- 所有人都基于这份 Hermes + 本插件使用

优点：
- 最少 debug
- 行为一致

### 方案 B：插件仓库只放桥接代码，同时附带本清单

也就是当前这个仓库的做法：
- 插件仓库负责桥本身
- `docs/HERMES_PATCH_CHECKLIST.md` 负责列出 Hermes 需要配合修改的点

优点：
- 仓库职责清楚

缺点：
- 使用者还是需要自己确认 Hermes 本体是否补齐

---

## 6. 不要误解的一点

这个仓库已经尽量把“插件侧”做得顺滑了：
- 安装脚本会自动初始化运行目录
- README 也写了完整安装、配置、私聊/群聊接法、排错

但如果 Hermes 本体还是 stock 官方版本、没有这些 `qq` 补丁：
- cron 可能不投递
- toolset 可能 KeyError
- allowlist 可能不生效
- QQ 平台可能根本不被认成一等公民
- 图片处理时还可能被危险命令审批卡住
- 改完 `config.yaml` 还可能重启错服务

所以如果你希望“别人拿到这个仓库就顺畅使用，而不是不停 debug”，最关键的其实不是 README 本身，而是：

**README 必须明确告诉使用者：Hermes 本体也要同步补这些点。**

这就是这份清单存在的意义。
