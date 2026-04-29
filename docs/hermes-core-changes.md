# Hermes core changes required by this plugin

Hermes currently does not expose a stable third-party platform-adapter registry.
For that reason a native QQ plugin needs a small Hermes core patch so the gateway
can discover `platforms.qq` and instantiate `NapCatQQAdapter`.

The installer applies these changes idempotently:

1. `gateway/config.py`
   - add `Platform.QQ = "qq"`
   - mark `platforms.qq.extra.onebot_url` / `onebot_ws_url` as a connected platform
   - keep official `qqbot` separate from personal QQ/NapCat

2. `gateway/run.py`
   - create `NapCatQQAdapter` when `Platform.QQ` is enabled
   - map QQ allowlist environment variables:
     - `QQ_ALLOWED_USERS`
     - `QQ_GROUP_ALLOWED_USERS`
     - `QQ_ALLOW_ALL_USERS`

3. `gateway/platforms/qq.py`
   - shim import that loads `hermes_qq.NapCatQQAdapter`

4. `gateway/platforms/__init__.py`
   - export `NapCatQQAdapter`

5. `hermes_cli/platforms.py`
   - register `qq` as `QQ Personal` with default toolset `hermes-qq`

6. `toolsets.py`
   - add the `hermes-qq` toolset
   - include it in the aggregate `hermes-gateway` toolset

7. `gateway/display_config.py` when available
   - treat QQ like other non-editable platforms: low-noise defaults

Recommended upstream direction: Hermes should eventually provide a stable
external platform adapter entrypoint, e.g. Python entry points such as
`hermes.gateway_platforms`, so this patch layer can disappear.
