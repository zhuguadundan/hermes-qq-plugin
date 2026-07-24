# 本地误删恢复手册

本文用于在本地插件仓库丢失、editable 安装失效或 Hermes 升级后恢复个人 QQ adapter。

## 恢复范围

GitHub 仓库可以恢复：

- `hermes_qq` 插件源代码
- Hermes shim
- 安装和 core patch 脚本
- 无真实凭据的配置示例
- 自动化测试和文档

GitHub 仓库不能恢复：

- `~/.hermes/config.yaml` 的真实配置和 token
- `~/.hermes` 下的会话、记忆、日志、cron、缓存
- NapCat 登录状态、设备文件和 NapCat 配置
- Hermes 主仓库中的其他未提交修改
- systemd 环境变量

请单独备份这些本机数据，并避免把 token 推送到 GitHub。

## 场景一：插件目录被误删

确认旧目录确实不存在：

```bash
test ! -e ~/hermes-qq-plugin && echo 'ready to restore'
```

重新克隆：

```bash
cd ~
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
```

安装到现有 Hermes：

```bash
./scripts/install-native-qq.sh ~/.hermes/hermes-agent
```

如果 `~/.hermes/config.yaml` 仍然存在，不要用示例文件覆盖它。只在缺少 QQ 配置时参考 `examples/config.qq.yaml` 手动合并。

重启并验证：

```bash
systemctl --user restart hermes-gateway.service
systemctl --user is-active hermes-gateway.service

~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import hermes_qq
print(hermes_qq.__file__)
PY
```

模块路径应为：

```text
/home/<你的用户名>/hermes-qq-plugin/hermes_qq/__init__.py
```

## 场景二：出现 `ModuleNotFoundError: hermes_qq`

最常见原因是 editable 安装仍指向已删除或移动的目录。进入恢复后的仓库重新安装：

```bash
cd ~/hermes-qq-plugin
~/.hermes/hermes-agent/venv/bin/python -m pip install --no-deps -e .
systemctl --user restart hermes-gateway.service
```

检查 editable 来源：

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip show hermes-qq-plugin
~/.hermes/hermes-agent/venv/bin/python -c 'import hermes_qq; print(hermes_qq.__file__)'
```

## 场景三：Hermes 主目录也重新安装过

重新运行完整安装器，让它恢复 shim 和能自动识别的 core 集成：

```bash
cd ~/hermes-qq-plugin
./scripts/install-native-qq.sh ~/.hermes/hermes-agent
```

仔细阅读输出。如果出现 `warn: marker not found`，根据 `docs/hermes-core-changes.md` 检查 Hermes 新版本，不要把 warning 当成成功。

## 场景四：配置文件也丢失

从 `examples/config.qq.yaml` 创建自己的配置，但必须重新填写：

- NapCat HTTP/WebSocket 地址
- NapCat access token
- 私聊 QQ 白名单
- 群号白名单
- home channel
- 需要的显示策略

示例文件故意不含真实凭据。恢复完成后建议先使用严格 allowlist，再逐步开放功能。

## 完整验收清单

### 1. 插件测试

```bash
cd ~/hermes-qq-plugin
~/.hermes/hermes-agent/venv/bin/python -m py_compile hermes_qq/*.py
~/.hermes/hermes-agent/venv/bin/python -m pytest -q
git diff --check
```

### 2. Hermes 集成测试

如果 Hermes 仓库包含对应测试：

```bash
cd ~/.hermes/hermes-agent
venv/bin/python -m pytest -q tests/gateway/test_native_qq_adapter.py
```

### 3. NapCat HTTP

```bash
curl -H 'Authorization: Bearer YOUR_NAPCAT_TOKEN' \
  http://127.0.0.1:3000/get_login_info
curl -H 'Authorization: Bearer YOUR_NAPCAT_TOKEN' \
  http://127.0.0.1:3000/get_status
```

期望账号正确，且状态包含 `online: true`、`good: true`。

### 4. Gateway 和 WebSocket

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
tail -n 200 ~/.hermes/logs/agent.log | grep -E 'QQ|NapCat|WebSocket|Traceback'
```

应看到 HTTP 登录成功和 WebSocket connected，不应看到 `ModuleNotFoundError`。

### 5. 实际消息

依次测试：

1. 白名单私聊文本；
2. 允许群内文本或 @ 消息；
3. 图片加文字；
4. 语音；
5. 文件；
6. 重启 Gateway 后再次发消息，确认 WebSocket 重连正常。

## 建议的额外备份

至少定期加密备份：

```text
~/.hermes/config.yaml
~/.config/systemd/user/hermes-gateway.service
NapCat 配置和设备目录
Hermes 主仓库中的未提交 diff
```

备份真实 token 的文件不要提交到公开 GitHub 仓库。
