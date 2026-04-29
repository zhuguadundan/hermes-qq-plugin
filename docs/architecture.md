# Architecture

This repository intentionally implements QQ as a native Hermes Gateway platform,
not as a bridge that shells out to `HermesCLI`.

## Runtime flow

```text
NapCat / OneBot 11
  ├─ HTTP API: send messages, upload files, fetch media
  └─ WebSocket events: inbound messages/notices
        ↓
hermes_qq.client.NapCatClient
        ↓
hermes_qq.adapter.NapCatQQAdapter
        ↓
Hermes gateway.platforms.base.MessageEvent
        ↓
Hermes GatewayRunner
        ↓
Hermes native sessions, commands, model switching, tools, memory, delivery
```

## Why not the old bridge?

The old bridge opened isolated CLI-style turns. That caused split-brain bugs:

- `/model` and WebUI model switches did not reliably affect QQ sessions.
- `/reasoning`, `/new`, `/reset`, `/stop` could diverge from native channels.
- every turn could look like a fresh CLI run instead of a gateway session.
- file/media handling lived outside Hermes' normal delivery path.

The native adapter only translates OneBot events and media into Hermes native
objects. All higher-level behavior stays in Hermes core.

## Package layout

- `hermes_qq/client.py` — NapCat/OneBot HTTP RPC client.
- `hermes_qq/types.py` — small shared dataclasses.
- `hermes_qq/adapter.py` — Hermes `BasePlatformAdapter` implementation.
- `gateway_platform_shim/qq.py` — shim copied into Hermes core import path.
- `scripts/patch_hermes_core.py` — idempotent Hermes core patcher.
- `scripts/install-native-qq.sh` — installs package + shim + core patch.
