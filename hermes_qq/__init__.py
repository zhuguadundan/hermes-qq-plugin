"""Native NapCat/OneBot QQ platform adapter for Hermes Gateway.

`NapCatQQAdapter` imports Hermes Gateway classes, so it is loaded lazily.  This
keeps the lightweight client module importable outside a Hermes checkout.
"""

from .client import NapCatClient, QQBridgeError
from .types import QQEventSource

__all__ = [
    "NapCatQQAdapter",
    "NapCatClient",
    "QQBridgeError",
    "QQEventSource",
    "check_qq_requirements",
]


def __getattr__(name: str):
    if name in {"NapCatQQAdapter", "check_qq_requirements"}:
        from .adapter import NapCatQQAdapter, check_qq_requirements

        return {
            "NapCatQQAdapter": NapCatQQAdapter,
            "check_qq_requirements": check_qq_requirements,
        }[name]
    raise AttributeError(name)
