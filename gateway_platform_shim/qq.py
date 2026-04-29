"""Shim installed into Hermes as gateway/platforms/qq.py.

The implementation lives in the external hermes_qq package so the QQ adapter can
be shared independently from the Hermes core repository while still satisfying
Hermes Gateway's native platform import path.
"""

from hermes_qq import (  # noqa: F401
    NapCatClient,
    NapCatQQAdapter,
    QQBridgeError,
    QQEventSource,
    check_qq_requirements,
)
