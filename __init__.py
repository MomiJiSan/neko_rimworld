"""Lazy SDK entry; importing the domain package does not load its host facade."""

_EXPORTS = {
    "NekoRimworldPlugin",
    "BridgeSettings",
    "PLUGIN_VERSION",
    "_parse_settings",
    "_agent_handoff",
    "_query_summary",
}
__all__ = ["NekoRimworldPlugin"]


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    from . import entry

    return getattr(entry, name)


def __dir__():
    return sorted(set(globals()) | _EXPORTS)
