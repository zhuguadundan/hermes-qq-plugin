# Hermes Core 集成说明

## 为什么需要修改 Hermes

当前 Hermes 没有稳定的第三方 Gateway platform entrypoint。仅安装 `hermes_qq` Python 包还不足以让 Gateway 识别 `platforms.qq`，因此本仓库提供：

- `gateway_platform_shim/qq.py`
- `scripts/patch_hermes_core.py`
- `scripts/install-native-qq.sh`

安装脚本先 editable 安装插件、复制 shim，再执行 core patcher。

## patcher 期望的改动

### `gateway/config.py`

- 增加 `Platform.QQ = "qq"`；
- 当 `platforms.qq.extra` 中存在 OneBot HTTP 或 WebSocket 地址时，将个人 QQ 识别为已配置平台；
- 保持个人 QQ `qq` 与腾讯官方机器人 `qqbot` 分离。

### `gateway/run.py`

- 为 `Platform.QQ` 创建 `NapCatQQAdapter`；
- 增加 QQ 私聊、群聊和 allow-all 环境变量映射：
  - `QQ_ALLOWED_USERS`
  - `QQ_GROUP_ALLOWED_USERS`
  - `QQ_ALLOW_ALL_USERS`

### `gateway/platforms/qq.py`

从外部包导入：

```python
from hermes_qq import NapCatQQAdapter, check_qq_requirements
```

### `gateway/platforms/__init__.py`

导出 `NapCatQQAdapter`。

### `hermes_cli/platforms.py`

注册 `qq`，显示为个人 QQ 平台，并使用 `hermes-qq` toolset。

### `toolsets.py`

- 注册 `hermes-qq` toolset；
- 将它加入 Gateway 聚合 toolset。

### `gateway/display_config.py`

在文件存在且标记匹配时，将 QQ 配置为不可编辑消息平台的低噪音默认策略。

## 幂等性和限制

`patch_hermes_core.py` 使用保守的文本标记替换：

- 已存在的改动会显示 `ok`，不会重复插入；
- 找不到标记会显示 `warn: marker not found`；
- 找不到标记时脚本不会猜测 Hermes 新版本结构，也不会盲目覆盖文件。

因此，`warn` 不是成功。脚本在补丁后校验 QQ 枚举、工厂、shim、导出、CLI 注册与工具集，必要集成缺失时返回非零退出码；安装器另外实际导入 adapter。Hermes 升级后仍应检查输出及集成测试。

只读检查现有安装：

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/patch_hermes_core.py --verify-only ~/.hermes/hermes-agent
```

## 安装后验证

```bash
~/.hermes/hermes-agent/venv/bin/python -c \
  'import hermes_qq; print(hermes_qq.__file__)'

grep -n 'QQ = "qq"' ~/.hermes/hermes-agent/gateway/config.py
grep -n 'NapCatQQAdapter' ~/.hermes/hermes-agent/gateway/run.py
ls -l ~/.hermes/hermes-agent/gateway/platforms/qq.py
```

运行测试：

```bash
cd ~/hermes-qq-plugin
~/.hermes/hermes-agent/venv/bin/python -m pytest -q

cd ~/.hermes/hermes-agent
venv/bin/python -m pytest -q tests/gateway/test_native_qq_adapter.py
```

重启并检查日志：

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
tail -n 200 ~/.hermes/logs/agent.log | grep -E 'QQ|NapCat|WebSocket'
```

成功启动通常能看到：

```text
[QQ] Connected to NapCat as ...
[QQ] WebSocket connected: ...
```

## Hermes 升级后的处理

1. 先备份 Hermes 主仓库中的未提交修改和 `~/.hermes/config.yaml`。
2. 完成 Hermes 升级。
3. 重新运行 `./scripts/install-native-qq.sh ~/.hermes/hermes-agent`。
4. 检查是否出现 `warn: marker not found`。
5. 运行插件测试和 Hermes QQ 集成测试。
6. 重启 Gateway 并观察 QQ 连接日志。

不要用本仓库覆盖 Hermes 主仓库中的其他本地修改。本仓库只负责 QQ plugin 及其明确列出的集成点。

## 长期方向

如果 Hermes 将来提供类似 Python entry points 的正式接口，例如 `hermes.gateway_platforms`，本插件应迁移到该接口，并删除 core 文本 patch 层。
