"""Compile the closed-enum semantic plan onto the Mod's public query contract.

The model never writes field names, native argument keys or block structure; this
module turns a validated plan into the same request the Mod already accepts, and
resolves field types, argument schemas and native notes from the schema directory
the Mod advertises.

This is the production copy of the compiler. It deliberately contains no
answer-side projection: the semantic interface renders results as plain text, so
the interning/projection helpers used by other prototypes are not here. The
inverse reader used for historical replay is also not here.
"""

import copy
import json

OPTIONS = {"args", "where", "select", "aggregate", "group_by", "order_by", "limit", "offset", "on"}


def compact(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class SurfaceError(ValueError):
    pass


class Surface:
    def __init__(self, directory):
        self.directory = directory
        self.roots = {r["from"]: r for r in directory["roots"]}
        self.index = directory["field_index"]
        self.branches = {}
        self.argument_schemas = {}
        self.notes = copy.deepcopy(directory.get("field_notes", {}))
        for t in directory.get("common_fields", []):
            self.notes[t["type"]] = {f["name"]: f.get("description") for f in t.get("fields", [])}

    def add_descriptions(self, descriptions):
        for item in descriptions:
            self.notes[item["type"]] = {f["name"]: f.get("description") for f in item.get("fields", [])}
            self.branches[item["type"]] = item.get("on", [])

    def add_query_schema(self, schema):
        branches = schema["properties"]["requests"]["items"]["allOf"][0]["oneOf"]
        self.argument_schemas = {b["properties"]["from"]["const"]: b["properties"]["args"] for b in branches}

    def discovery_reply(self, source, name):
        matches = self.discover(source, name)
        return dict(
            matches=matches[:20],
            more_matches=len(matches) > 20,
            source=self.roots[source],
            argument_schema=self.argument_schemas.get(source),
            native_query_guide=self.directory["rules"],
        )

    def child_type(self, kind, field):
        spec = self.index.get(kind, {}).get(field)
        if spec is None:
            raise SurfaceError("Unknown field " + kind + "." + field)
        return spec.split(" ")[0].strip("[]!")

    def path_type(self, root, path):
        kind = self.roots[root]["type"]
        for segment in path.split(".") if path else []:
            name, sep, concrete = segment.partition("@")
            kind = self.child_type(kind, name)
            if sep:
                if concrete not in self.index:
                    raise SurfaceError("Unknown concrete type: " + concrete)
                # Actual allowed polymorphic branches remain validated by Mod.
                kind = concrete
        return kind

    def fields(self, root, path="", include_notes=True):
        kind = self.path_type(root, path)
        value = dict(source=root, at=path, type=kind, fields=self.index.get(kind, {}))
        if include_notes:
            value["meanings"] = self.notes.get(kind, {})
        return value

    def discover(self, root, name, depth=5):
        """Find paths mechanically by field name, not natural-language intent."""
        found = []

        def visit(kind, path, chain, left):
            if left < 0 or kind not in self.index:
                return
            for field in self.index[kind]:
                at = ".".join(filter(None, [path, field]))
                child = self.child_type(kind, field)
                if name.casefold() in field.casefold():
                    domain = (
                        self.index[kind][field].split(" domain:")[-1] if " domain:" in self.index[kind][field] else None
                    )
                    found.append(
                        dict(
                            path=at,
                            type=self.index[kind][field],
                            meaning=self.notes.get(kind, {}).get(field),
                            value_domain=self.directory.get("value_domains", {}).get(domain),
                        )
                    )
                if child not in chain:
                    visit(child, at, (*chain, child), left - 1)
            for concrete in self.branches.get(kind, []):
                if path and concrete not in chain:
                    visit(concrete, path + "@" + concrete, (*chain, concrete), left - 1)

        visit(self.roots[root]["type"], "", (self.roots[root]["type"],), depth)
        return found

    def flatten(self, request):
        root = request["from"]
        if root not in self.roots:
            raise SurfaceError("Unknown root")
        blocks = []

        def read_selection(kind, select):
            result = {}

            def add(name, value):
                if name in result:
                    raise SurfaceError("Overlapping selected field: " + name)
                result[name] = copy.deepcopy(value)

            if isinstance(select, dict):
                for key, value in select.items():
                    add(key, value)
            elif isinstance(select, list):
                for part in select:
                    if isinstance(part, dict):
                        for key, value in part.items():
                            add(key, value)
                    elif isinstance(part, str):
                        keys = part.split(".")
                        cursor = result
                        for key in keys[:-1]:
                            if key not in cursor:
                                cursor[key] = {"select": {}}
                            if not isinstance(cursor[key], dict) or not isinstance(cursor[key].get("select"), dict):
                                raise SurfaceError("Overlapping path")
                            cursor = cursor[key]["select"]
                        if keys[-1] in cursor:
                            raise SurfaceError("Overlapping path leaf")
                        cursor[keys[-1]] = True
                    else:
                        raise SurfaceError("Unsupported selection member")
            else:
                raise SurfaceError("Expected selected fields")
            return result

        def walk(kind, node, at):
            if set(node) - (OPTIONS | {"from"}):
                raise SurfaceError("Unknown selection options")
            block = {"at": at, **{k: copy.deepcopy(v) for k, v in node.items() if k not in {"from", "select", "on"}}}
            blocks.append(block)
            if "select" in node:
                if node["select"] is True:
                    block["fields"] = True
                else:
                    selected = read_selection(kind, node["select"])
                    block["fields"] = []
                    for field, value in selected.items():
                        child = self.child_type(kind, field)
                        if value is True:
                            block["fields"].append(field)
                            continue
                        path = ".".join(filter(None, [at, field]))
                        if isinstance(value, list):
                            walk(child, {"select": value}, path)
                        elif isinstance(value, dict):
                            bare = (
                                bool(value)
                                and not set(value) & OPTIONS
                                and set(value) <= set(self.index.get(child, {}))
                            )
                            walk(child, {"select": value} if bare else value, path)
                        else:
                            raise SurfaceError("Selected fields require true or a selection")
            if "on" in node:
                if not node["on"]:
                    block["empty_on"] = True
                for concrete, selection in node["on"].items():
                    if not at:
                        raise SurfaceError("Root polymorphic branches are unsupported")
                    bare = isinstance(selection, dict) and bool(selection) and not set(selection) & OPTIONS
                    body = {"select": selection} if isinstance(selection, list) or bare else selection
                    walk(concrete, body, at + "@" + concrete)

        walk(self.roots[root]["type"], request, "")
        return dict(source=root, blocks=blocks)

    def compile(self, command):
        root = command["source"]
        if root not in self.roots or not isinstance(command.get("blocks"), list) or len(command["blocks"]) > 512:
            raise SurfaceError("Invalid source/blocks")
        result = {"from": root}
        assigned = set()

        def node_at(path):
            self.path_type(root, path)
            node = result
            for segment in path.split(".") if path else []:
                name, branch, kind = segment.partition("@")
                selected = node.setdefault("select", {})
                if not isinstance(selected, dict):
                    raise SurfaceError("Defaults cannot be merged with explicit children")
                node = selected.setdefault(name, {})
                if not isinstance(node, dict):
                    raise SurfaceError("Overlapping scalar/default selection")
                if branch:
                    node = node.setdefault("on", {}).setdefault(kind, {})
            return node

        for block in command["blocks"]:
            path = block["at"]
            if path in assigned:
                raise SurfaceError("Duplicate block: " + path)
            assigned.add(path)
            if len(path.split(".")) > 13:
                raise SurfaceError("Path too deep")
            node = node_at(path)
            for key, value in block.items():
                if key == "at":
                    continue
                if key == "fields":
                    if value is True:
                        if node.get("select"):
                            raise SurfaceError("Overlapping default selection")
                        node["select"] = True
                    else:
                        node.setdefault("select", {})
                        for field in value:
                            parent, dot, leaf = field.rpartition(".")
                            field_path = ".".join(filter(None, [path, parent])) if dot else path
                            name = leaf if dot else field
                            selected = node_at(field_path).setdefault("select", {})
                            if not isinstance(selected, dict) or name in selected:
                                raise SurfaceError("Overlapping field")
                            self.child_type(self.path_type(root, field_path), name)
                            selected[name] = True
                elif key == "empty_on":
                    node["on"] = {}
                elif key in OPTIONS - {"select", "on"}:
                    node[key] = copy.deepcopy(value)
                else:
                    raise SurfaceError("Unknown block option: " + key)
        return result
