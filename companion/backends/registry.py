"""Registry and factory for BrokerBackend implementations."""

from __future__ import annotations

from typing import Any

from .base import BrokerBackend

_REGISTRY: dict[str, type[BrokerBackend]] = {}


def register_backend(name: str, cls: type[BrokerBackend]) -> None:
    """Register a BrokerBackend implementation class by name.

    Args:
        name: Backend identifier string (case-insensitive).
        cls: BrokerBackend subclass.

    Raises:
        TypeError: If cls is not a subclass of BrokerBackend.
    """
    if not isinstance(cls, type) or not issubclass(cls, BrokerBackend):
        raise TypeError(f"{cls} must be a subclass of BrokerBackend")
    _REGISTRY[name.lower()] = cls


from .schwab import SchwabBackend

register_backend("schwab", SchwabBackend)


def get_backend(name: str, **kwargs: Any) -> BrokerBackend:
    """Instantiate and return a registered BrokerBackend.

    Args:
        name: Name of the broker backend to instantiate (case-insensitive).
        **kwargs: Arguments forwarded to the backend constructor.

    Returns:
        BrokerBackend instance.

    Raises:
        ValueError: If no backend is registered under `name`.
    """
    key = name.lower()
    if key not in _REGISTRY:
        if key == "schwab":
            try:
                from .schwab import SchwabBackend
                register_backend("schwab", SchwabBackend)
            except ImportError:
                pass

    if key not in _REGISTRY:
        available = sorted(_REGISTRY.keys())
        raise ValueError(f"Unknown broker backend: '{name}'. Available: {available}")

    backend_cls = _REGISTRY[key]
    return backend_cls(**kwargs)
