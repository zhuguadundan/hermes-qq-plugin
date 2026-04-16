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
