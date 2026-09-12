"""Render Mod-described, already observed values. No game queries or domain calculations."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping, Sequence

VERSION = "observed_values.v1"


class EvidenceError(ValueError):
    pass


def _parts(path: str) -> list[str]:
    if not isinstance(path, str) or len(path) > 2048 or (path and not path.startswith("/")):
        raise EvidenceError("Expected a bounded path into this observation's data.")
    if re.search(r"~(?![01])", path):
        raise EvidenceError("Paths use only literal JSON Pointer escapes ~0 and ~1.")
    return [] if path == "" else [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _get(data: Any, path: str) -> Any:
    value = data
    for part in _parts(path):
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(value):
                raise EvidenceError("Array path was not returned: " + path)
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise EvidenceError("Field was not returned: " + path)
    return value


def _path(parent: str, key: str) -> str:
    return parent + "/" + key.replace("~", "~0").replace("/", "~1")


def _pattern(path: str) -> str:
    return re.sub(r"/\d+(?=/|$)", "/~2", path)


def _concrete(pattern: str, selected: str) -> str:
    indices = iter(re.findall(r"/(\d+)(?=/|$)", selected))
    try:
        return re.sub(r"/~2(?=/|$)", lambda _: "/" + next(indices), pattern)
    except StopIteration:
        raise EvidenceError("Result description requires an unavailable array position.") from None


def _text(value: Any) -> str:
    # Labels are quoted data, never Markdown/HTML instructions or supplied column semantics.
    raw = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )
    literal = (
        str(raw).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ").replace("\r", " ")
    )
    return re.sub(r"([\\`*_\[\]!#|])", r"\\\1", literal)


def render_evidence(
    refs: object, observations: Sequence[Mapping[str, Any]], language: str, *, _seen: set[tuple[str, str]] | None = None
) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(refs, list) or len(refs) > 4:
        raise EvidenceError("evidence must be an array of at most 4 observation references.")
    if not refs:
        return "", "", {"format": "evidence_text.v1", "renderer_version": VERSION, "sources": []}
    index: dict[str, Mapping[str, Any]] = {}
    for observation in observations:
        identity = observation.get("observation_id")
        if not isinstance(identity, str) or identity in index:
            raise EvidenceError("Observation identities are missing or ambiguous.")
        index[identity] = observation
    latest = observations[-1].get("response", {}) if observations else {}
    seen_readings = _seen if _seen is not None else set()
    used: list[str] = []
    full_blocks: list[str] = []
    brief_blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    zh = language == "zh"
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) - {"observation_id", "brief_paths"}:
            raise EvidenceError("Each reference accepts observation_id and optional brief_paths only.")
        identity = ref.get("observation_id")
        if not isinstance(identity, str) or identity not in index or identity in used:
            raise EvidenceError("Reference must name one unique observation from this run.")
        observation = index[identity]
        response = observation.get("response", {})
        for key in ("connection_epoch", "colony_id", "load_epoch"):
            if response.get(key) != latest.get(key):
                raise EvidenceError(
                    "Observation belongs to an earlier connection or load; obtain current evidence or discuss it explicitly as historical text."
                )
        payload = response.get("payload", {})
        descriptor = payload.get("execution", {}).get("evidence", {})
        data = payload.get("data")
        if (
            payload.get("status") not in {"ok", "partial"}
            or not isinstance(data, dict)
            or data.get("metadata_only") is True
        ):
            raise EvidenceError("Failed requests and metadata cannot be cited as game state.")
        if descriptor.get("version") != VERSION or not isinstance(descriptor.get("nodes"), dict):
            raise EvidenceError(
                "This observation lacks the shared result description; use a text submission or a compatible Mod."
            )
        nodes = descriptor["nodes"]
        paths = ref.get("brief_paths", [""])
        if not isinstance(paths, list) or not 1 <= len(paths) <= 32 or any(not isinstance(p, str) for p in paths):
            raise EvidenceError("brief_paths requires 1..32 unique returned paths.")
        paths = list(dict.fromkeys(path if not path or path.startswith("/") else "/" + path for path in paths))
        for path in paths:
            _get(data, path)

        def description(path: str) -> dict[str, Any]:
            node = nodes.get(path, nodes.get(_pattern(path)))
            if not isinstance(node, dict) or node.get("kind") not in {"object", "collection", "scalar", "reading"}:
                raise EvidenceError("Result description is missing for " + path)
            if "context_ref" in node:
                attached = descriptor.get("contexts", {}).get(node["context_ref"])
                if not isinstance(attached, dict):
                    raise EvidenceError("Shared result context is missing.")
                return {**node, "context": attached}
            return node

        def resolve(path: str | None, selected: str) -> Any:
            return _get(data, _concrete(path, selected)) if path is not None else None

        shared_context: dict[str, Any] = {}
        shared_unit: str | None = None

        def context(node: dict[str, Any], path: str, *, brief: bool) -> str:
            def dereference(value: Any) -> Any:
                if isinstance(value, dict):
                    if set(value) == {"path"}:
                        return resolve(value["path"], path)
                    if set(value) == {"source"}:
                        source_path = _concrete(value["source"], path)
                        subject = descriptor.get("subjects", {}).get(source_path)
                        if not isinstance(subject, dict):
                            raise EvidenceError("Result subject description is missing.")
                        return subject
                    return {key: dereference(child) for key, child in value.items()}
                return value

            values = node.get("context", {})
            if not isinstance(values, dict):
                raise EvidenceError("Invalid result context.")
            return "; ".join(
                _text(key) + "=" + _text(dereference(value))
                for key, value in values.items()
                if key != "scope_rule" and not (key in shared_context and value == shared_context[key])
            )

        def unavailable(path: str) -> bool:
            for issue in payload.get("issues", []):
                issue_path = ""
                for part in issue.get("path", []):
                    issue_path = _path(issue_path, str(part))
                if path == issue_path or path.startswith(issue_path + "/"):
                    return True
            return False

        def render(path: str, *, brief: bool, rendered: set[tuple[str, str]], depth: int = 0) -> str:
            if depth > 32:
                raise EvidenceError("Presentation exceeds the structural depth limit.")
            node = description(path)
            if node.get("bound_to"):
                return render(_concrete(node["bound_to"], path), brief=brief, rendered=rendered, depth=depth + 1)
            if (identity, path) in rendered:
                return ""
            rendered.add((identity, path))
            value = _get(data, path)
            name = _text(node.get("native") or node.get("name") or path)
            prefix = context(node, path, brief=brief)
            if node["kind"] == "reading":
                reading = node.get("reading", {})
                amount = resolve(reading.get("value"), path)
                lower = resolve(reading.get("lower"), path)
                state = resolve(reading.get("state"), path)
                present = resolve(reading.get("present"), path)
                if reading.get("present") is not None:
                    result = (
                        ("无条目" if zh else "no entry")
                        if present is False
                        else (
                            _text(amount) if present is True and amount is not None else ("未知" if zh else "unknown")
                        )
                    )
                elif state is True or state == "complete":
                    if type(amount) not in {int, float}:
                        raise EvidenceError("An exact reading is missing its numeric value.")
                    result = _text(amount)
                else:
                    result = "精确值未知" if zh else "exact value unknown"
                    if lower is not None:
                        if type(lower) not in {int, float}:
                            raise EvidenceError("Invalid lower bound.")
                        result += ("，至少" if zh else ", at least ") + _text(lower)
                unit = reading.get("unit")
                if not isinstance(unit, str) or not unit:
                    raise EvidenceError("Reading unit is missing.")
                return (
                    (prefix + ": " if prefix else "")
                    + result
                    + ("" if brief and unit == shared_unit else " " + _text(unit))
                )
            if isinstance(value, dict):
                body = "; ".join(
                    filter(
                        None,
                        (render(_path(path, key), brief=brief, rendered=rendered, depth=depth + 1) for key in value),
                    )
                )
                if value and not body:
                    return ""
                return (prefix + ": " if prefix else "") + name + "{" + body + "}"
            if isinstance(value, list):
                body = "; ".join(
                    filter(
                        None,
                        (
                            render(_path(path, str(i)), brief=brief, rendered=rendered, depth=depth + 1)
                            for i in range(len(value))
                        ),
                    )
                )
                if value and not body:
                    return ""
                return (prefix + ": " if prefix else "") + name + "[" + body + "]"
            result = (
                ("不可得" if zh else "unavailable")
                if unavailable(path)
                else (_text(value) if value is not None else ("null（不适用）" if zh else "null (not applicable)"))
            )
            return (prefix + ": " if prefix else "") + name + "=" + result

        # A selected scalar is bound to its Mod-declared reading. Never let a caller re-label its range.
        normalized = [_concrete(description(path).get("bound_to", path), path) for path in paths]
        # Identity-equivalent selections (e.g. value + its status) are one
        # displayed reading. This is structural union, never semantic ranking.
        normalized = list(dict.fromkeys(normalized))
        normalized = [path for path in normalized if (identity, path) not in seen_readings]
        if not normalized:
            continue
        descriptions = [description(path) for path in normalized]
        if descriptions:
            shared_context = {
                key: value
                for key, value in descriptions[0].get("context", {}).items()
                if key != "scope_rule" and all(other.get("context", {}).get(key) == value for other in descriptions)
            }
            # Array-relative identities must not be shared across different subjects.
            shared_context = {key: value for key, value in shared_context.items() if "/~2" not in json.dumps(value)}
            units = [node.get("reading", {}).get("unit") for node in descriptions]
            if units[0] and all(unit == units[0] for unit in units):
                shared_unit = units[0]
        seen_before = set(seen_readings)
        full_seen = set(seen_readings)
        brief_seen = set(seen_readings)
        full = "\n".join(filter(None, (render(path, brief=False, rendered=full_seen) for path in normalized)))
        saved_shared = shared_context
        shared_context = {}
        shared_header = context({"context": saved_shared}, normalized[0], brief=False) if saved_shared else ""
        shared_context = saved_shared
        if shared_unit:
            shared_header += ("; " if shared_header else "") + ("单位=" if zh else "unit=") + _text(shared_unit)
        brief = (shared_header + ": " if shared_header else "") + "; ".join(
            filter(None, (render(path, brief=True, rendered=brief_seen) for path in normalized))
        )
        seen_readings.update(full_seen)
        sample = {key: response.get(key) for key in ("colony_id", "load_epoch", "game_tick", "snapshot_sequence")}
        sample_label = ("采样 tick=" if zh else "sample tick=") + _text(sample["game_tick"])
        rules = []
        for oid, location in full_seen - seen_before:
            if oid == identity:
                rule = description(location).get("context", {}).get("scope_rule")
                if isinstance(rule, str) and rule not in rules:
                    rules.append(rule)
        full_header = ("采样=" if zh else "sample=") + _text(sample)
        if shared_header:
            full_header += "\n" + shared_header
        full_blocks.append(
            full_header + "\n" + full + ("\n" + "\n".join(_text(rule) for rule in rules) if rules else "")
        )
        brief_blocks.append(sample_label + ": " + brief)
        used.append(identity)
        sources.append({"observation_id": identity, "paths": copy.deepcopy(paths), "sample": sample})
    return (
        "\n\n".join(full_blocks),
        "\n".join(brief_blocks),
        {"format": "evidence_text.v1", "renderer_version": VERSION, "sources": sources},
    )
