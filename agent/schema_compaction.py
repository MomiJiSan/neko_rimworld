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
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#"):
            # References into a subschema's interior depend on its original
            # property path. Factoring that node would invalidate the pointer.
            prefixes = ("#/$defs/", "#/definitions/")
            if not any(reference.startswith(prefix) and "/" not in reference[len(prefix):] for prefix in prefixes):
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
    # Keep their visited form: merging the unvisited originals back here used
    # to silently undo all factoring inside named definitions.
    definitions = copy.deepcopy(output.get("$defs", {}))
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


def deduplicate_descriptions(tools):
    """Publish identical parameter explanations once; never alter validation."""
    seen = set()

    def walk(node, property_name=None, bindings=""):
        if isinstance(node, list):
            return [walk(value, property_name, bindings) for value in node]
        if not isinstance(node, dict):
            return node
        result = copy.deepcopy(node)
        description = node.get("description")
        if property_name is not None and isinstance(description, str):
            # Distinct actor/user fields cannot authorize each other's prose.
            # The full schema node must also match: equal words with different
            # types, enums, defaults, constraints or nested properties stay local.
            encoded = canonical(node)
            key = (property_name, encoded, bindings if '"$ref"' in encoded else "")
            if key in seen:
                result.pop("description", None)
            else:
                seen.add(key)
        for field, value in node.items():
            if field in MAPS and isinstance(value, dict):
                result[field] = {k: walk(v, k if field == "properties" else property_name, bindings) for k, v in value.items()}
            elif field in ARRAYS and isinstance(value, list):
                result[field] = [walk(v, property_name, bindings) for v in value]
            elif field in SINGLES:
                result[field] = walk(value, property_name, bindings)
        return result

    result = copy.deepcopy(tools)
    for tool in result:
        function = tool.get("function", {})
        if isinstance(function.get("parameters"), dict):
            parameters = function["parameters"]
            bindings = canonical({k: parameters[k] for k in ("$defs", "definitions", "$id") if k in parameters})
            function["parameters"] = walk(parameters, bindings=bindings)
    return result


def schema_contract(schema):
    """Keep every validation keyword and property; omit prose annotations."""
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    result = copy.deepcopy(schema)
    for key in ("description", "title", "examples"):
        result.pop(key, None)
    for key, value in schema.items():
        if key in MAPS and isinstance(value, dict):
            result[key] = {name: schema_contract(child) for name, child in value.items()}
        elif key in ARRAYS and isinstance(value, list):
            result[key] = [schema_contract(child) for child in value]
        elif key in SINGLES:
            result[key] = schema_contract(value)
    return result


def remove_redundant_constraints(schema):
    """Remove unreachable definitions and types already entailed by enum/const.

    This is only used for the model's parameter contract, never native dispatch.
    Scoped and dynamic schemas retain their definition graph unchanged.
    """
    result = copy.deepcopy(schema)

    def accepts_type(value, kind):
        if isinstance(kind, list):
            return any(accepts_type(value, item) for item in kind)
        return {
            "string": isinstance(value, str), "boolean": type(value) is bool,
            "null": value is None, "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "number": type(value) in (int, float),
            "integer": type(value) is int or type(value) is float and value.is_integer(),
        }.get(kind, False)

    def simplify(node):
        if not isinstance(node, dict):
            return
        choices = node.get("enum")
        if "const" in node:
            choices = [node["const"]]
        if isinstance(choices, list) and choices and "type" in node and all(
            accepts_type(value, node["type"]) for value in choices
        ):
            node.pop("type")
        for child in children(node):
            simplify(child)

    simplify(result)
    definitions = result.get("$defs")
    if not isinstance(definitions, dict):
        return result

    def scoped(node):
        if not isinstance(node, dict):
            return False
        ref = node.get("$ref")
        interior = isinstance(ref, str) and ref.startswith("#") and (
            not ref.startswith("#/$defs/") or "/" in ref[len("#/$defs/"):]
        )
        return interior or any(key in node for key in ("$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveRef")) or any(scoped(child) for child in children(node))

    if scoped(result):
        return result
    reachable = set()

    def references(node):
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/"):].split("/", 1)[0].replace("~1", "/").replace("~0", "~")
            if name in definitions and name not in reachable:
                reachable.add(name)
                references(definitions[name])
        for key, value in node.items():
            if key == "$defs":
                continue
            if key in MAPS and isinstance(value, dict):
                for child in value.values():
                    references(child)
            elif key in ARRAYS and isinstance(value, list):
                for child in value:
                    references(child)
            elif key in SINGLES:
                references(value)

    references(result)
    result["$defs"] = {name: node for name, node in definitions.items() if name in reachable}
    if not result["$defs"]:
        result.pop("$defs")
    return result
