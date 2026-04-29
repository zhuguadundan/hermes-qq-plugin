# Hermes QQ Plugin — Native NapCat / OneBot Personal QQ Adapter

This repository provides a **native Hermes Gateway platform adapter** for
personal QQ accounts through [NapCat](https://napneko.github.io/) / OneBot 11.

It is not the old `HermesCLI` bridge.  The adapter turns OneBot events into
Hermes native `MessageEvent`s, so QQ shares the same session store, commands,
model switching, toolsets, media delivery, hooks, memory, and transcript logic as
Telegram, Discord, Weixin, and other built-in Hermes messaging platforms.

## Why this exists

A standalone QQ bridge that starts isolated CLI turns creates hard-to-debug
behavior:

- model switches in config or WebUI do not reliably affect QQ conversations;
- `/new`, `/model`, `/reasoning`, `/stop`, `/reset` diverge from native channels;
- image + follow-up text can race into separate turns;
- progress/status messages can leak implementation details into QQ groups;
- file/media delivery is duplicated outside Hermes' normal delivery path.

This plugin keeps QQ as a thin transport adapter and leaves the actual AI/session
behavior in Hermes core.

## Current status

- Personal QQ via NapCat / OneBot 11: supported.
- Private chat: supported.
- Group chat: supported, including allowlists and optional respond-to-all mode.
- Text, image, voice/audio, video, document/file receive: supported where NapCat
  exposes usable URLs or file metadata.
- Text, image, voice/audio, video, document/file send: supported through OneBot
  send APIs and NapCat file upload helpers.
- Official QQ Bot API: **not** this plugin.  Keep using Hermes `qqbot` for that.

## Repository layout

```text
hermes_qq/
  adapter.py      # Hermes BasePlatformAdapter implementation
  client.py       # NapCat/OneBot HTTP RPC client
  types.py        # shared dataclasses

gateway_platform_shim/
  qq.py           # copied to Hermes gateway/platforms/qq.py

scripts/
  install-native-qq.sh      # install package + shim + Hermes core patch
  patch_hermes_core.py      # idempotent core patcher

examples/
  config.qq.yaml            # config.yaml snippet

docs/
  architecture.md           # design rationale
  hermes-core-changes.md    # exact Hermes core integration points
```

## Prerequisites

1. A working Hermes checkout, usually:

   ```bash
   /home/USER/.hermes/hermes-agent
   ```

2. Hermes virtualenv installed:

   ```bash
   ~/.hermes/hermes-agent/venv/bin/python
   ```

3. NapCat running with OneBot HTTP + WebSocket enabled.  Example endpoints:

   - HTTP: `http://127.0.0.1:3000`
   - WebSocket: `ws://127.0.0.1:3001`

4. A NapCat token if your NapCat instance requires one.

## Install

Clone this repository and run the installer against your Hermes checkout:

```bash
git clone https://github.com/zhuguadundan/hermes-qq-plugin.git
cd hermes-qq-plugin
./scripts/install-native-qq.sh ~/.hermes/hermes-agent
```

The installer does three things:

1. installs this package into Hermes' virtualenv with `pip install -e`;
2. copies `gateway_platform_shim/qq.py` to Hermes as `gateway/platforms/qq.py`;
3. applies the minimal Hermes core patch needed to discover and load
   `platforms.qq`.

Then restart Hermes Gateway:

```bash
systemctl --user restart hermes-gateway.service
systemctl --user status hermes-gateway.service --no-pager
```

If you do not use systemd, restart whatever command runs:

```bash
python -m hermes_cli.main gateway run --replace
```

## Configure Hermes

Merge `examples/config.qq.yaml` into `~/.hermes/config.yaml`.

Minimal private-chat config:

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

Group config:

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
        - '987654321'
      group_chat_all: true
      group_sessions_per_user: false
    home_channel:
      platform: qq
      chat_id: group:987654321
      name: QQ group 987654321

  qqbot:
    enabled: false
```

Recommended display defaults for QQ:

```yaml
display:
  platforms:
    qq:
      tool_progress: off
      streaming: false
      interim_assistant_messages: false
      show_reasoning: false
```

QQ cannot edit messages, so permanent progress/interim bubbles are intentionally
quiet by default.  Necessary user-facing command replies still go to chat.

## NapCat / OneBot setup checklist

In NapCat, enable:

- OneBot HTTP server;
- OneBot WebSocket server;
- the same access token configured in Hermes;
- image/file access or download URLs if you want media receive;
- file upload APIs if you want Hermes to send documents/files.

Then verify:

```bash
curl -H 'Authorization: Bearer YOUR_NAPCAT_TOKEN' \
  http://127.0.0.1:3000/get_login_info
```

## Commands

Once installed as a native platform, QQ uses Hermes Gateway commands:

- `/new` or `/reset` — reset the current QQ session;
- `/model` / `/model MODEL_NAME` — inspect or switch model;
- `/reasoning` / `/reasoning high` — inspect or change reasoning effort;
- `/stop` — stop current run;
- `/status` — status;
- `/help` — help.

Model changes in Hermes config or WebUI should apply to QQ because QQ is no
longer an isolated CLI bridge.

## What changes are required in Hermes core?

Hermes does not yet expose a stable external platform-adapter registry, so this
plugin currently requires a small core patch.  See:

- [`docs/hermes-core-changes.md`](docs/hermes-core-changes.md)
- [`scripts/patch_hermes_core.py`](scripts/patch_hermes_core.py)

Summary:

- add `Platform.QQ = "qq"`;
- load `NapCatQQAdapter` in `GatewayRunner._create_adapter()`;
- register QQ allowlist env vars;
- add `qq` to CLI platform metadata;
- add `hermes-qq` toolset;
- install `gateway/platforms/qq.py` shim;
- set low-noise display defaults for QQ.

The preferred long-term Hermes change is an official external adapter entrypoint
such as Python entry points (`hermes.gateway_platforms`).  When Hermes supports
that, this repository can stop patching core files.

## Troubleshooting

### Gateway does not load QQ

Check:

```bash
grep -n "QQ =" ~/.hermes/hermes-agent/gateway/config.py
ls ~/.hermes/hermes-agent/gateway/platforms/qq.py
~/.hermes/hermes-agent/venv/bin/python -c 'import hermes_qq; print(hermes_qq.NapCatQQAdapter)'
```

Then restart gateway.

### QQ messages do not trigger replies

Check allowlists:

- private chats: `platforms.qq.extra.allow_from`
- groups: `platforms.qq.extra.group_allow_from`
- `group_chat_all`; if false, bot only responds when mentioned.

### Files do not send

File sending depends on NapCat file APIs.  Verify the bot account can upload
files in that chat and that NapCat exposes `upload_file_stream`,
`upload_group_file`, and/or `send_online_file`.

### Seeing internal status in QQ groups

QQ cannot edit messages, so use:

```yaml
display:
  platforms:
    qq:
      tool_progress: off
      streaming: false
      interim_assistant_messages: false
```

This hides noisy progress bubbles while preserving meaningful command replies.

## Development

Run lightweight tests:

```bash
python -m pytest -q
```

When developing inside a Hermes checkout, also run Hermes gateway tests that
cover native QQ behavior.

## License

MIT
