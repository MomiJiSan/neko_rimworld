"""Default ordinary-JSON context path; historical codecs are not runtime modes."""

from __future__ import annotations

import os
import warnings

from .serialization import canonical as canonical
from .serialization import digest as digest

MAX_SOURCE_CHARS = 4 * 1024 * 1024
CONTEXT_TOOLS = frozenset(
    {
        "look",
        "count",
        "inspect_space",
        "inspect_route",
        "survey_space",
        "inspect_state",
        "inspect_threats",
        "evaluate_activity",
        "execute_python",
        "read_result",
    }
)
RETIRED_DEFAULTS = {
    "NEKO_CONTEXT_PRUNING": True,
    "NEKO_ACTIVITY": True,
    "NEKO_DEF_NAME_RECOVERY": True,
    "NEKO_PUBLIC_REFERENCES": False,
}


def validate_deployment():
    """Reject old variants; matching old values only receive migration warnings."""
    for name, expected in RETIRED_DEFAULTS.items():
        value = os.environ.get(name, "").strip().lower()
        if not value:
            continue
        matching = {"1", "true", "yes", "on"} if expected else {"0", "false", "no", "off"}
        if value not in matching:
            raise ValueError(f"{name} is retired; historical variants require frozen source.")
        warnings.warn(f"{name} is retired and redundant; remove it from the environment.", DeprecationWarning, stacklevel=2)
    for name in ("NEKO_EVIDENCE_COMPACT", "NEKO_PUBLIC_INPUT_TOKEN_LIMIT"):
        if os.environ.get(name, "").strip():
            raise ValueError(f"{name} is retired; use frozen experiments or bench-owned budgets.")


def make_pruner():
    from .context_pruning import ContextPruner

    return ContextPruner()


def model_size(value, *, arguments=None, context_enabled=None):
    """Standalone size, independent of earlier messages and note suppression."""
    selected = True if context_enabled is None else context_enabled
    return make_pruner().compile(value, arguments=arguments).view_chars if selected else len(canonical(value))
