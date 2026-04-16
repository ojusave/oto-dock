"""Client adapter registry.

Adapters are registered at startup and looked up by client_type string
(sent via X-Claude-Client header or inferred from transport).
"""

from adapters.base import ClientAdapter

_registry: dict[str, ClientAdapter] = {}


def register_adapter(adapter: ClientAdapter) -> None:
    """Register an adapter instance by its name."""
    _registry[adapter.name] = adapter


def get_adapter(client_type: str) -> ClientAdapter | None:
    """Get the adapter for a client type, or None if unknown."""
    return _registry.get(client_type)


def get_session_adapter(session_id: str) -> ClientAdapter | None:
    """Look up a session's client_type and return its adapter."""
    from core.session.session_state import get_session_client_type
    client_type = get_session_client_type(session_id)
    return _registry.get(client_type)
