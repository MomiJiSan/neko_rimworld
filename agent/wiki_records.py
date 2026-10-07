"""Select literal field paths and return whole records within a byte budget."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

_PATH = re.compile(r"[\w$]+(?:\[\d+(?:,\d+)*\])?(?:\.[\w$]+(?:\[\d+(?:,\d+)*\])?)*")


def byte_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def bounded_text(value: str, limit: int) -> str:
    value = value.encode("utf-8", "replace").decode("utf-8")
    while byte_size(value) > limit + 2:
        value = value[: max(0, len(value) - max(1, len(value) // 4))]
    return value


def paths(text: str) -> list[str]:
    """Literal suffix fields, including Unicode and reflected metadata names."""
    return list(dict.fromkeys(_PATH.findall(text)))


def under(path: str, prefix: str) -> bool:
    path, prefix = path.casefold(), prefix.casefold()
    return path == prefix or path.startswith((prefix + ".", prefix + "["))


def group_path(path: str) -> str:
    # Keep the nearest indexed record's values together. Scalars have no group.
    if "]" in path:
        return path[: path.rfind("]") + 1]
    return path.rsplit(".", 1)[0] if "." in path else path


def select_groups(facts: list[dict], requested: list[str]) -> tuple[list[tuple[str, list[dict]]], list[str]]:
    if not requested:
        grouped: dict[str, list[dict]] = {}
        for fact in facts:
            grouped.setdefault(group_path(fact["name"]), []).append(fact)
        # Ordinary records precede null and reflection metadata groups, without
        # imposing any game-specific whitelist or changing values inside a group.
        return sorted(
            grouped.items(),
            key=lambda item: all(f.get("type") == "null" or f["name"].endswith((".$type", ".$ref")) for f in item[1]),
        ), []
    prefixes, missing = [], []
    for request in requested:
        matching = [f for f in facts if under(f["name"], request)]
        if not matching:
            missing.append(request)
            continue
        exact = next((f["name"] for f in matching if f["name"].casefold() == request.casefold()), None)
        prefixes.append(group_path(exact) if exact else request)
    # Coalesce overlaps before budgeting so products and products[0].count don't
    # produce misleading fragments or duplicate facts, regardless of query order.
    prefixes = list(dict.fromkeys(prefixes))
    prefixes = [p for p in prefixes if not any(p != q and under(p, q) for q in prefixes)]
    return [(p, [f for f in facts if under(f["name"], p)]) for p in prefixes], missing


def directory(facts: list[dict], prefixes: list[str], limit: int = 48) -> tuple[list[dict], bool]:
    """Real immediate children; repeat a path to inspect the next level."""
    nodes: dict[str, dict] = {}
    for prefix in prefixes or [""]:
        for fact in facts:
            name = fact["name"]
            if prefix and not under(name, prefix):
                continue
            suffix = name[len(prefix) :].lstrip(".")
            if not suffix:
                child = name
            else:
                match = re.match(r"\[\d+(?:,\d+)*\]|[^.\[]+", suffix)
                if not match:
                    continue
                child = prefix + ("." if prefix and not suffix.startswith("[") else "") + match.group(0)
            node = nodes.setdefault(child, {"path": child, "kind": "group"})
            if name == child:
                node["kind"] = "null" if fact.get("type") == "null" else "scalar"
            elif name == child + ".count":
                node.update(kind="collection", count=fact["value"])
    values = list(nodes.values())
    return values[:limit], len(values) > limit


def render_entry(entry: dict, requested: list[str], budget: int, read_label: Callable[[str], str | None]) -> dict:
    """No fact/path/type is clipped. Omitted groups stay distinct from nulls."""
    result = {
        key: bounded_text(entry[key], 768 if key == "description" else 256)
        for key in ("type", "def_name", "label", "description", "source_mod", "package_id")
    }
    result["key"] = entry["key"]
    metadata_cut = any(result[key] != entry[key] for key in result)
    if len(entry["key"]) > 4096 or byte_size(entry["key"]) > budget - 1400:
        result.update(
            key=None, key_unavailable="exceeds_query_limit" if len(entry["key"]) > 4096 else "result_byte_limit"
        )
        metadata_cut = True
    diagnostics = sorted(entry.get("diagnostics", []), key=lambda d: d["kind"] != "incomplete")
    result["diagnostics"] = [{k: bounded_text(d[k], 128) for k in ("path", "kind", "reason")} for d in diagnostics[:4]]
    result["diagnostics_truncated"] = result["diagnostics"] != diagnostics
    result["source_truncated"] = entry["truncated"] or any(d["kind"] == "incomplete" for d in diagnostics)
    groups, missing = select_groups(entry["facts"], requested)
    result.update(
        facts=[],
        groups=[],
        related=[],
        related_truncated=False,
        missing_fields=missing,
        not_read=[],
        not_read_count=0,
        read_complete=False,
        directory=[],
        directory_truncated=False,
        field_names=[],
        response_truncated=False,
        truncated=False,
        context_not_read=[],
        context_not_read_count=0,
    )
    selected: set[str] = set()

    def append_group(path: str, rows: list[dict]) -> bool:
        # Test the whole response, not just the new rows. Reserve navigation
        # space and never manufacture a clipped fact or a comma-joined path.
        start = len(result["facts"])
        result["facts"].extend(dict(f) for f in rows)
        result["groups"].append({"path": path, "start": start, "count": len(rows)})
        if byte_size(result) > budget - 1200:
            del result["facts"][start:]
            result["groups"].pop()
            return False
        selected.update(f["name"].casefold() for f in rows)
        return True

    # Reserve all explicit leaves before expanding any parent. This also works
    # when a caller requests both a large subtree and a leaf inside that tree.
    requests = {p.casefold() for p in requested}
    for fact in entry["facts"]:
        if fact["name"].casefold() in requests and fact["name"].casefold() not in selected:
            append_group(fact["name"], [fact])
    narrowed = []
    if requested:
        # Complete smaller requested groups before larger ones; an ingredient
        # filter must not consume the space needed by a product/count record.
        groups.sort(key=lambda item: (byte_size(item[1]), item[0]))
    for path, original in groups:
        remaining = [f for f in original if f["name"].casefold() not in selected]
        if remaining and not append_group(path, remaining):
            narrowed.append(path)

    # Read completeness describes what was requested. Optional sibling context
    # can be omitted while every explicitly requested leaf is still complete.
    omitted = [
        p
        for p in requested
        if p not in missing
        and any(under(f["name"], p) and f["name"].casefold() not in selected for f in entry["facts"])
    ]
    if not requested:
        omitted = narrowed[:]
    result["context_not_read"] = narrowed[:8]
    result["context_not_read_count"] = len(narrowed)
    result["read_complete"] = not omitted and not missing and not result["source_truncated"]
    result["not_read_count"] = len(omitted)
    result["not_read"] = omitted[:8]
    if omitted:
        # Navigation is useful for unread requested subtrees, not for already
        # satisfied leaves or a missing path. Keep context omissions as markers.
        result["directory"], result["directory_truncated"] = directory(entry["facts"], omitted or narrowed)
        result["field_names"] = [n["path"] for n in result["directory"]]

    # Labels are optional context too; they cannot displace requested values.
    labels = 0
    for fact in result["facts"]:
        value = fact["value"]
        if "/" in value and fact.get("type") not in {"System.String", "System.Type", "null"} and labels < 12:
            labels += 1
            label = read_label(value)
            if label:
                fact["label"] = bounded_text(label, 256)
                if byte_size(result) > budget - 100:
                    del fact["label"]
    # Directed references are optional extra context after requested facts.
    for relation in entry["related"][:4]:
        key = relation["key"]
        row = {"key": key, "label": bounded_text(relation["label"], 128)}
        if len(key) > 4096 or byte_size(key) > budget // 2:
            row.update(key=None, key_unavailable="exceeds_query_limit" if len(key) > 4096 else "result_byte_limit")
        result["related"].append(row)
        if byte_size(result) > budget - 100:
            result["related"].pop()
            break
    result["related_truncated"] = result["related"] != entry["related"]
    result["response_truncated"] = bool(omitted or narrowed or metadata_cut or result["related_truncated"])
    result["truncated"] = result["response_truncated"] or result["source_truncated"]
    while byte_size(result) > budget and result["directory"]:
        result["directory"].pop()
        result["field_names"] = [n["path"] for n in result["directory"]]
        result["directory_truncated"] = True
    # Unusually long navigation paths must not consume the whole response.
    for field in ("not_read", "context_not_read", "missing_fields"):
        while byte_size(result) > budget and result[field]:
            result[field].pop()
            result[field + "_truncated"] = True
    return result
