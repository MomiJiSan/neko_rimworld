"""Factor identical JSON Schema subschemas; keep annotations and constraints."""

from __future__ import annotations

import copy
import json
from collections import Counter
from functools import lru_cache

MAPS = frozenset({"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"})
ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
SINGLES = frozenset(
    {"items", "additionalProperties", "contains", "not", "if", "then", "else", "propertyNames", "unevaluatedProperties"}
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def children(schema):
    for key, value in schema.items():
        if key in MAPS and isinstance(value, dict):
            yield from (v for v in value.values() if isinstance(v, dict))
        elif key in ARRAYS and isinstance(value, list):
            yield from (v for v in value if isinstance(v, dict))
        elif key in SINGLES and isinstance(value, dict):
            yield value


@lru_cache(maxsize=128)
def _compact(text):
    root = json.loads(text)
    counts, values = Counter(), {}

    def structure(node):
        # Per-use descriptions remain beside the reference. The common schema
        # retains all validation keywords and every other annotation.
        return {k: v for k, v in node.items() if k != "description"}

    def count(node):
        if not isinstance(node, dict):
            return
        # Moving a scoped/anchored subschema can change reference resolution.
        if any(k in node for k in ("$id", "$anchor", "$dynamicRef", "$recursiveRef")):
            raise ValueError("scoped_schema")
        key = canonical(structure(node))
        counts[key] += 1
        values[key] = structure(node)
        for value in children(node):
            count(value)

    try:
        count(root)
    except (ValueError, RecursionError):
        return text
    definitions = copy.deepcopy(root.get("$defs", {}))
    if not isinstance(definitions, dict):
        return text
    chosen = {}
    for key, occurrences in counts.items():
        if occurrences <= 1:
            continue
        name = f"c{len(chosen)}"
        while name in definitions or name in chosen.values():
            name += "_"
        reference = canonical({"$ref": "#/$defs/" + name})
        if (occurrences - 1) * len(key) > occurrences * len(reference) + len(name) + 24:
            chosen[key] = name

    def visit(node, defining=None):
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        key = canonical(structure(node))
        if key in chosen and key != defining:
            return {
                "$ref": "#/$defs/" + chosen[key],
                **({"description": node["description"]} if "description" in node else {}),
            }
        result = copy.deepcopy(node)
        for field, value in node.items():
            if field in MAPS and isinstance(value, dict):
                result[field] = {k: visit(v) for k, v in value.items()}
            elif field in ARRAYS and isinstance(value, list):
                result[field] = [visit(v) for v in value]
            elif field in SINGLES:
                result[field] = visit(value)
        return result

    output = visit(root)
    # Existing definitions retain their original names and relative scope.
    for key, name in chosen.items():
        definitions[name] = visit(values[key], key)
    if definitions:
        output["$defs"] = {**output.get("$defs", {}), **definitions}
    generated = set(chosen.values())
    original_names = set(root.get("$defs", {}))
    # Factoring a large parent can make its child definitions redundant. Prune
    # unreachable generated definitions, then inline single-use ones. Existing
    # named definitions are retained even when not referenced by this root.
    for _ in range(len(generated) + 1):
        defs = output.get("$defs", {})
        uses = Counter()
        seen = set()

        def references(node):
            if isinstance(node, list):
                for v in node:
                    references(v)
            elif isinstance(node, dict):
                ref = node.get("$ref", "")
                name = ref[len("#/$defs/") :] if isinstance(ref, str) and ref.startswith("#/$defs/") else None
                if name in generated:
                    uses[name] += 1
                    if name not in seen:
                        seen.add(name)
                        references(defs[name])
                for k, v in node.items():
                    if k != "$defs":
                        references(v)

        references({k: v for k, v in output.items() if k != "$defs"})
        for name in original_names:
            references(defs[name])
        dead = generated - set(uses)
        for name in dead:
            defs.pop(name, None)
        generated -= dead
        single = {name for name in generated if uses[name] == 1}
        if not single:
            break

        def inline(node):
            if isinstance(node, list):
                return [inline(v) for v in node]
            if not isinstance(node, dict):
                return node
            ref = node.get("$ref", "")
            name = ref[len("#/$defs/") :] if isinstance(ref, str) and ref.startswith("#/$defs/") else None
            if name in single:
                return inline({**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}})
            return {k: inline(v) for k, v in node.items()}

        output = inline(output)
        for name in single:
            output["$defs"].pop(name, None)
        generated -= single
    if not output.get("$defs"):
        output.pop("$defs", None)
    encoded = canonical(output)
    return encoded if len(encoded) < len(text) else text


def compact_schema(schema):
    return json.loads(_compact(canonical(schema)))


def compact_tools(tools):
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool.get("function", {})
        if isinstance(function.get("parameters"), dict):
            function["parameters"] = compact_schema(function["parameters"])
    return result
