"""Rank observed path metrics, never infer an unknown path or an ETA."""

import copy
import math


def rank_paths(rows, by):
    rows = copy.deepcopy(rows)
    meaning = (
        "Each row retains its own sample and scope; these are separate observations, "
        "not an atomic snapshot. Ranking covers only these returned paths, not global "
        "nearest objects or fastest jobs. Unavailable paths have no rank; zero is a valid length."
    )
    if by is None:
        return {"results": rows, "meaning": meaning}
    known, unranked, actors = [], [], set()
    for row in rows:
        data = row["result"]
        route = data.get("route") or {}
        value = route.get(by)
        unit_ok = (
            by == "steps"
            or isinstance(value, dict)
            and value.get("unit") == {"path_length": "map_cell_widths", "native_cost": "native_path_cost"}[by]
        )
        if isinstance(value, dict):
            value = value.get("value")
        actor = (data.get("from") or {}).get("id")
        if (
            not unit_ok
            or route.get("status") != "found"
            or type(value) not in (float, int)
            or not math.isfinite(value)
            or value < 0
            or not actor
        ):
            row["rank"] = None
            unranked.append(row)
        else:
            actors.add(actor)
            known.append((value, row))
    if len(actors) > 1:
        for row in rows:
            row["rank"] = None
        return {
            "results": rows,
            "ordering": {"by": by, "status": "unavailable", "reason": "actor_identity_changed"},
            "meaning": meaning,
        }
    known.sort(key=lambda pair: pair[0])
    last, rank = None, 0
    for i, (value, row) in enumerate(known):
        if i == 0 or value != last:
            rank = i + 1
        row["rank"] = rank
        last = value
    return {
        "results": [row for _, row in known] + unranked,
        "ordering": {
            "by": by,
            "direction": "ascending",
            "ranked": len(known),
            "unranked": len(unranked),
            "complete": not unranked,
            "ties": "equal values share competition rank",
        },
        "meaning": meaning,
    }
