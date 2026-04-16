"""Proxy-side phone-server adapters (control plane).

Public surface: the ABC + dataclasses + error from ``base``, and the
``load_adapter`` factory. See ``base.py`` for the adapter contract and
``manual_asterisk.py`` for the reference implementation.
"""

from .base import (
    BootstrapResult,
    HealthStatus,
    PhoneAdapterError,
    PhoneServerAdapter,
    RouteHandle,
)
from .loader import ADAPTER_CLASSES, available_adapter_types, load_adapter

__all__ = [
    "PhoneServerAdapter",
    "HealthStatus",
    "BootstrapResult",
    "RouteHandle",
    "PhoneAdapterError",
    "load_adapter",
    "ADAPTER_CLASSES",
    "available_adapter_types",
]
