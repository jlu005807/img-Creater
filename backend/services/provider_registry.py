from __future__ import annotations

from typing import Any, Callable


class ProviderRegistry:
    """Registry mapping api_type strings to provider execution methods.

    This decouples the dispatch logic in ImageService._run_provider from the
    concrete method names, enabling future extraction of providers into
    separate adapter classes without changing the dispatch site.
    """

    # Per-class handler registry.  Using __init_subclass__ ensures each
    # subclass gets its own dict instead of silently sharing the parent's
    # mutable class attribute.
    _handlers: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._handlers = {}

    @classmethod
    def register(cls, api_type: str, method_name: str) -> Callable:
        """Register a method name for the given api_type."""
        normalized = api_type.strip().lower()

        def decorator(func: Callable) -> Callable:
            cls._handlers[normalized] = method_name
            return func

        return decorator

    @classmethod
    def get_handler(cls, api_type: str) -> str | None:
        """Return the registered method name for api_type, or None."""
        return cls._handlers.get(api_type.strip().lower())

    @classmethod
    def dispatch(
        cls,
        service: Any,
        api_type: str,
        provider: dict[str, Any],
        payload: dict[str, Any],
        operation: str,
        deadline: float | None = None,
    ) -> tuple[list[str], Any, dict[str, Any]]:
        """Dispatch to the registered handler method on the given service."""
        method_name = cls.get_handler(api_type) or cls._handlers.get("openai")
        if method_name is None:
            raise ValueError(f"No provider registered for api_type={api_type!r}")
        method = getattr(service, method_name)
        return method(provider, payload, operation, deadline)

    @classmethod
    def registered_types(cls) -> list[str]:
        """Return all registered api_type strings."""
        return list(cls._handlers.keys())
