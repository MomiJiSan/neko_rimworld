"""Experimental ordering of world observations; values and paths are unchanged."""

from __future__ import annotations

import copy


def _ordered(value, first, last=()):
    if not isinstance(value, dict):
        return value
    keys = [key for key in first if key in value]
    keys.extend(key for key in value if key not in first and key not in last)
    keys.extend(key for key in last if key in value)
    return {key: value[key] for key in keys}


def order_world_fields(data):
    """Return a detached model view, preserving every field, value and array order."""
    result = copy.deepcopy(data)
    if not isinstance(result, dict):
        return result
    rows = result.get("objects")
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("facets"), dict):
            continue
        facets = row["facets"]
        if "pawn" in facets:
            facets["pawn"] = _ordered(
                facets["pawn"],
                (
                    "status",
                    "downed",
                    "dead",
                    "conditions",
                    "conditions_scope",
                    "damaged_present_parts",
                    "missing_parts",
                    "capacities",
                    "pain_fraction",
                    "bleed_fraction_per_game_day",
                ),
                ("health_summary_fraction",),
            )
        row["facets"] = _ordered(facets, ("pawn", "stats", "work", "bed", "door", "reservations", "condition"))
    return result
