#!/usr/bin/env python3
"""Best-effort idempotent Hermes core patcher for the native QQ adapter.

This script patches a Hermes checkout so it can load the external ``hermes_qq``
package as a first-class Gateway platform.  It intentionally uses conservative
text edits because Hermes does not yet expose a stable external platform-adapter
plugin registry.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def replace(path: Path, old: str, new: str, label: str) -> bool:
    text = read(path)
    if new in text:
        print(f"ok: {label} already present")
        return False
    if old not in text:
        print(f"warn: marker not found for {label}: {path}")
        return False
    write(path, text.replace(old, new, 1))
    print(f"patched: {label}")
    return True


def insert_before(path: Path, marker: str, block: str, label: str) -> bool:
    text = read(path)
    if block.strip() in text:
        print(f"ok: {label} already present")
        return False
    if marker not in text:
        print(f"warn: marker not found for {label}: {path}")
        return False
    write(path, text.replace(marker, block + marker, 1))
    print(f"patched: {label}")
    return True


def patch_config(root: Path) -> None:
    path = root / "gateway" / "config.py"
    replace(
        path,
        '    BLUEBUBBLES = "bluebubbles"\n    QQBOT = "qqbot"',
        '    BLUEBUBBLES = "bluebubbles"\n    QQ = "qq"\n    QQBOT = "qqbot"',
        "Platform.QQ enum",
    )
    replace(
        path,
        '            # QQBot uses extra dict for app credentials\n            elif platform == Platform.QQBOT and config.extra.get("app_id") and config.extra.get("client_secret"):',
        '            # Personal QQ uses NapCat/OneBot endpoints.\n            elif platform == Platform.QQ and (\n                config.extra.get("onebot_url") or config.extra.get("onebot_ws_url")\n            ):\n                connected.append(platform)\n            # Official QQBot uses extra dict for app credentials\n            elif platform == Platform.QQBOT and config.extra.get("app_id") and config.extra.get("client_secret"):',
        "QQ connected-platform detection",
    )


def patch_run(root: Path) -> None:
    path = root / "gateway" / "run.py"
    insert_before(
        path,
        '        elif platform == Platform.QQBOT:\n',
        '        elif platform == Platform.QQ:\n'
        '            from gateway.platforms.qq import NapCatQQAdapter, check_qq_requirements\n'
        '            if not check_qq_requirements():\n'
        '                logger.warning("QQ: requests/websockets missing or NapCat OneBot not configured")\n'
        '                return None\n'
        '            return NapCatQQAdapter(config)\n\n',
        "GatewayRunner QQ adapter factory",
    )
    replace(
        path,
        '            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOWED_USERS",\n            Platform.QQBOT: "QQ_ALLOWED_USERS",',
        '            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOWED_USERS",\n            Platform.QQ: "QQ_ALLOWED_USERS",\n            Platform.QQBOT: "QQ_ALLOWED_USERS",',
        "QQ DM allowlist env map",
    )
    replace(
        path,
        '            Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_USERS",\n            Platform.QQBOT: "QQ_GROUP_ALLOWED_USERS",',
        '            Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_USERS",\n            Platform.QQ: "QQ_GROUP_ALLOWED_USERS",\n            Platform.QQBOT: "QQ_GROUP_ALLOWED_USERS",',
        "QQ group allowlist env map",
    )
    replace(
        path,
        '            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOW_ALL_USERS",\n            Platform.QQBOT: "QQ_ALLOW_ALL_USERS",',
        '            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOW_ALL_USERS",\n            Platform.QQ: "QQ_ALLOW_ALL_USERS",\n            Platform.QQBOT: "QQ_ALLOW_ALL_USERS",',
        "QQ allow-all env map",
    )


def patch_platform_exports(root: Path) -> None:
    path = root / "gateway" / "platforms" / "__init__.py"
    replace(path, 'from .qqbot import QQAdapter\n', 'from .qqbot import QQAdapter\nfrom .qq import NapCatQQAdapter\n', "QQ adapter export import")
    replace(path, '    "SendResult",\n    "QQAdapter",', '    "SendResult",\n    "NapCatQQAdapter",\n    "QQAdapter",', "QQ adapter __all__")


def patch_cli_platforms(root: Path) -> None:
    path = root / "hermes_cli" / "platforms.py"
    insert_before(
        path,
        '    ("qqbot",          PlatformInfo(label="💬 QQBot",           default_toolset="hermes-qqbot")),\n',
        '    ("qq",             PlatformInfo(label="🐧 QQ Personal",     default_toolset="hermes-qq")),\n',
        "CLI platform registry entry",
    )


def patch_toolsets(root: Path) -> None:
    path = root / "toolsets.py"
    insert_before(
        path,
        '    "hermes-qqbot": {\n',
        '    "hermes-qq": {\n'
        '        "description": "QQ personal-account toolset - NapCat/OneBot messaging via native gateway (full access)",\n'
        '        "tools": _HERMES_CORE_TOOLS,\n'
        '        "includes": []\n'
        '    },\n\n',
        "hermes-qq toolset",
    )
    replace(
        path,
        '"hermes-weixin", "hermes-qqbot", "hermes-webhook"',
        '"hermes-weixin", "hermes-qq", "hermes-qqbot", "hermes-webhook"',
        "hermes-gateway includes hermes-qq",
    )


def patch_display(root: Path) -> None:
    path = root / "gateway" / "display_config.py"
    if not path.exists():
        return
    replace(
        path,
        '    "dingtalk":        _TIER_LOW,\n\n    # Tier 4',
        '    "dingtalk":        _TIER_LOW,\n    "qq":              _TIER_LOW,\n\n    # Tier 4',
        "QQ low-noise display defaults",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_root", nargs="?", default=str(Path.home() / ".hermes" / "hermes-agent"))
    args = parser.parse_args()
    root = Path(args.hermes_root).expanduser().resolve()
    if not (root / "gateway" / "run.py").exists():
        raise SystemExit(f"Not a Hermes checkout: {root}")
    patch_config(root)
    patch_run(root)
    patch_platform_exports(root)
    patch_cli_platforms(root)
    patch_toolsets(root)
    patch_display(root)
    print("done: Hermes core QQ patch pass complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
