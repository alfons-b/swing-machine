"""Data source registry and permission gate."""

from idx_trading.registry.gate import is_permitted, require_permission
from idx_trading.registry.loader import RegistryFile, SourceSpec, load_registry, sync_registry

__all__ = [
    "RegistryFile",
    "SourceSpec",
    "is_permitted",
    "load_registry",
    "require_permission",
    "sync_registry",
]
