"""Rebuildable local SQLite index. Full Def records stay on disk.

Import owns the source file; consultations only pin immutable database paths.
No SQLite connection is shared across threads and no source import runs in query().
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
from typing import Any

import ijson
from ijson.common import ObjectBuilder

MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
INDEX_VERSION = 2
REFRESH_INTERVAL_SECONDS = 5.0


def signature(value: os.stat_result) -> list[int]:
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def validate_metadata(value: dict) -> None:
    if (value.get("format") != "neko.defs.v1"
            or any(not isinstance(value.get(k), str) for k in ("game_version", "language", "generated_at"))
            or type(value.get("complete")) is not bool
            or not isinstance(value.get("mods"), list)
            or not isinstance(value.get("issues"), list)):
        raise ValueError("invalid knowledge metadata")
    for field in ("issues", "warnings"):
        if field in value and (not isinstance(value[field], list) or any(not isinstance(x, str) for x in value[field])):
            raise ValueError("invalid diagnostics")
    if "provenance_complete" in value and type(value["provenance_complete"]) is not bool:
        raise ValueError("invalid provenance")
    for mod in value["mods"]:
        if not isinstance(mod, dict) or any(not isinstance(mod.get(k), str) for k in ("package_id", "name")):
            raise ValueError("invalid mod metadata")


def validate_entry(entry: Any) -> None:
    if (not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) for k in
            ("key", "type", "def_name", "label", "description", "source_mod", "package_id"))
            or not entry["type"] or not entry["def_name"]
            or entry["key"] != entry["type"] + "/" + entry["def_name"]
            or type(entry.get("truncated")) is not bool):
        raise ValueError("invalid definition")
    for field, keys in (("facts", ("name", "value")), ("related", ("key", "label")),
                        ("diagnostics", ("path", "kind", "reason"))):
        rows = entry.get(field, [] if field == "diagnostics" else None)
        if not isinstance(rows, list):
            raise ValueError("invalid definition rows")
        for row in rows:
            if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in keys)
                    or field == "facts" and "type" in row and not isinstance(row["type"], str)):
                raise ValueError("invalid definition row")


def search_text(entry: dict) -> tuple[list[str], str]:
    names = [entry["def_name"].casefold(), entry["label"].casefold()]
    descriptions = [entry["description"].casefold()]
    for fact in entry["facts"]:
        leaf = fact["name"].rsplit(".", 1)[-1].casefold()
        if leaf == "label":
            names.append(fact["value"].casefold())
        elif leaf == "description":
            descriptions.append(fact["value"].casefold())
    return list(dict.fromkeys(n for n in names if n)), " ".join(descriptions)


def records(stream):
    """Yield (root property or 'entry', value), independent of JSON key order.

    ObjectBuilder only holds one entry or one metadata value, never entries[].
    Duplicate root properties and malformed record envelopes fail the import.
    """
    builder = None
    level = 0
    target = None
    root_key = None
    seen = set()
    in_entries = False
    root_started = False
    root_ended = False
    allowed = {"format", "game_version", "language", "generated_at", "mods", "complete",
               "provenance_complete", "issues", "warnings", "entries"}
    for prefix, event, value in ijson.parse(stream):
        if not root_started:
            if prefix != "" or event != "start_map":
                raise ValueError("knowledge root must be an object")
            root_started = True
            continue
        if builder is not None:
            builder.event(event, value)
            if event in ("start_map", "start_array"):
                level += 1
            elif event in ("end_map", "end_array"):
                level -= 1
            if level == 0:
                yield target, builder.value
                builder = None
            continue
        if prefix == "" and event == "map_key":
            if value in seen or value not in allowed:
                raise ValueError("duplicate or unsupported root property")
            seen.add(value)
            root_key = value
            continue
        if root_key == "entries" and prefix == "entries" and event == "start_array":
            in_entries = True
            continue
        if in_entries:
            if prefix == "entries" and event == "end_array":
                in_entries = False
                continue
            if prefix != "entries.item" or event != "start_map":
                raise ValueError("entry must be an object")
            target = "entry"
        elif prefix == "" and event == "end_map":
            root_ended = True
            continue
        else:
            if root_key == "entries":
                raise ValueError("entries must be an array")
            target = root_key
        builder = ObjectBuilder()
        builder.event(event, value)
        level = 1 if event in ("start_map", "start_array") else 0
        if level == 0:
            yield target, builder.value
            builder = None
    if not root_started or not root_ended or builder is not None or "entries" not in seen:
        raise ValueError("incomplete knowledge file")


class KnowledgeView:
    """Small immutable-version handle; each read owns its connection."""
    def __init__(self, path: Path, metadata: dict):
        self.path = path
        self.metadata = metadata
        self._rows = None
        self._rows_lock = threading.Lock()

    def _connect(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.execute("PRAGMA cache_size=-2048")
        return connection

    def search_rows(self) -> list[dict]:
        # Shared immutable-generation directory only; payloads are never cached.
        with self._rows_lock:
            if self._rows is None:
                connection = self._connect()
                try:
                    self._rows = [{"key": key, "names": json.loads(names), "description": description}
                                  for key, names, description in connection.execute("SELECT key,names,description FROM search")]
                finally:
                    connection.close()
            return self._rows

    def read_entry(self, key: str) -> dict | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT payload FROM entries WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            connection.close()


class IndexedKnowledgeView(KnowledgeView):
    """Publisher-built directory; payload reads verify the selected entry hash."""
    MAX_INDEX_BYTES = 32 * 1024 * 1024

    def __init__(self, directory: Path):
        index_path = directory / "ready.json"
        fd = os.open(index_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > self.MAX_INDEX_BYTES:
                raise ValueError("invalid knowledge index")
            raw = source.read(self.MAX_INDEX_BYTES + 1)
            if len(raw) > self.MAX_INDEX_BYTES or signature(before) != signature(os.fstat(source.fileno())):
                raise ValueError("knowledge index changed")
        index = json.loads(raw)
        if not isinstance(index, dict) or index.get("format") != "neko.defs.index.v1":
            raise ValueError("invalid knowledge index format")
        filename = index.get("data_file", "")
        if not isinstance(filename, str) or not re.fullmatch(r"snapshot-[0-9a-f]{32}\.json", filename):
            raise ValueError("invalid knowledge payload name")
        self.path = (directory / filename).resolve()
        if self.path.parent != directory.resolve():
            raise ValueError("knowledge payload outside directory")
        total = index.get("data_bytes")
        if type(total) is not int or not 0 < total <= MAX_SNAPSHOT_BYTES:
            raise ValueError("invalid knowledge payload size")
        payload_stat = self.path.stat()
        if not stat.S_ISREG(payload_stat.st_mode) or payload_stat.st_size != total:
            raise ValueError("knowledge payload not ready")
        self._payload_signature = signature(payload_stat)
        metadata = index.get("snapshot")
        if not isinstance(metadata, dict):
            raise ValueError("invalid knowledge metadata")
        validate_metadata(metadata)
        digest = index.get("data_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid payload checksum")
        if metadata.get("sha256") != digest:
            raise ValueError("inconsistent payload checksum")
        self.metadata = metadata
        entries = index.get("entries")
        if not isinstance(entries, list) or len(entries) > 100000:
            raise ValueError("invalid knowledge directory")
        self._locations = {}
        self._rows = []
        previous_end = 0
        for row in entries:
            if not isinstance(row, dict):
                raise ValueError("invalid knowledge row")
            key, offset, length, checksum = (row.get(k) for k in ("key", "offset", "length", "sha256"))
            names, description = row.get("names"), row.get("description")
            if (not isinstance(key, str) or not key or key in self._locations
                    or type(offset) is not int or type(length) is not int
                    or offset < previous_end or not 0 < length <= 8 * 1024 * 1024 or offset + length > total
                    or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                    or not isinstance(names, list) or any(not isinstance(n, str) for n in names)
                    or not isinstance(description, str)):
                raise ValueError("invalid knowledge record index")
            self._locations[key] = (offset, length, checksum)
            self._rows.append({"key": key, "names": list(dict.fromkeys(n.casefold() for n in names if n)),
                               "description": description.casefold()})
            previous_end = offset + length
        self.index_signature = signature(before)

    def search_rows(self) -> list[dict]:
        return self._rows

    def read_entry(self, key: str) -> dict | None:
        location = self._locations.get(key)
        if location is None:
            return None
        offset, length, digest = location
        with self.path.open("rb") as source:
            if signature(os.fstat(source.fileno())) != self._payload_signature:
                raise ValueError("knowledge payload changed")
            source.seek(offset)
            data = source.read(length)
            if len(data) != length or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("knowledge entry checksum failed")
            if signature(os.fstat(source.fileno())) != self._payload_signature:
                raise ValueError("knowledge payload changed during read")
        entry = json.loads(data)
        validate_entry(entry)
        if entry["key"] != key:
            raise ValueError("knowledge entry identity mismatch")
        return entry


class KnowledgeLibrary:
    def __init__(self, directory: Path, cache_directory: Path | None = None, *, allow_legacy_import: bool = True):
        self.allow_legacy_import = allow_legacy_import
        self.source = Path(directory) / "current.json"
        self.cache = (Path(cache_directory) if cache_directory is not None else Path(directory) / ".index").resolve()
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._stop = threading.Event()
        self._pointer = None
        self._view = None
        self.last_error: str | None = None
        self._restore()
        # Publisher directory is small enough for immediate startup availability.
        # Never trigger the legacy full-file importer from construction.
        if (self.source.parent / "ready.json").is_file():
            self.refresh()

    def _restore(self):
        try:
            pointer = json.loads((self.cache / "current.json").read_text())
            filename = pointer.get("database", "")
            if (pointer.get("index_version") != INDEX_VERSION
                    or not re.fullmatch(r"[0-9a-f]{64}\.sqlite3", filename)):
                return
            path = self.cache / filename
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                metadata = json.loads(connection.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()[0])
            finally:
                connection.close()
            validate_metadata(metadata)
            self._pointer = pointer
            self._view = KnowledgeView(path, metadata)
        except (OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    def open_view(self) -> KnowledgeView | None:
        with self._lock:
            return self._view

    def request_refresh(self) -> None:
        """Never wait for an import on the consultation path."""
        with self._lock:
            if self._closed or self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._watch, name="neko-knowledge-import", daemon=True)
            self._thread.start()

    def _watch(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(REFRESH_INTERVAL_SECONDS)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop.set()
        # Import checks closed between records; don't block plugin shutdown.

    def refresh(self) -> bool:
        with self._refresh_lock:
            if self._closed:
                return False
            temporary = None
            connection = None
            try:
                ready = self.source.parent / "ready.json"
                if ready.exists():
                    if (isinstance(self._view, IndexedKnowledgeView)
                            and self._view.index_signature == signature(ready.stat())
                            and self._view._payload_signature == signature(self._view.path.stat())):
                        return True
                    view = IndexedKnowledgeView(self.source.parent)
                    with self._lock:
                        if self._closed:
                            return False
                        self._view = view
                        self.last_error = None
                    return True
                if not self.allow_legacy_import:
                    self.last_error = "knowledge_index_not_ready"
                    return False
                fd = os.open(self.source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SNAPSHOT_BYTES:
                        raise ValueError("invalid source file")
                    stamp = signature(before)
                    with self._lock:
                        if self._pointer is not None and self._pointer["signature"] == stamp:
                            return True
                    self.cache.mkdir(parents=True, exist_ok=True)
                    handle, name = tempfile.mkstemp(prefix="import-", suffix=".sqlite3", dir=self.cache)
                    os.close(handle)
                    temporary = Path(name)
                    connection = sqlite3.connect(temporary)
                    connection.execute("PRAGMA cache_size=-4096")
                    connection.execute("CREATE TABLE entries(key TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                    connection.execute("CREATE TABLE search(key TEXT PRIMARY KEY,names TEXT NOT NULL,description TEXT NOT NULL)")
                    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    metadata = {}
                    # Accept legacy BOM while hashing original bytes below.
                    source.seek(3 if source.read(3) == b"\xef\xbb\xbf" else 0)
                    for key, value in records(source):
                        if self._closed:
                            raise ValueError("import cancelled")
                        if key != "entry":
                            metadata[key] = value
                            continue
                        validate_entry(value)
                        names, description = search_text(value)
                        connection.execute("INSERT INTO entries VALUES(?,?)", (
                            value["key"], json.dumps(value, ensure_ascii=False, separators=(",", ":"))))
                        connection.execute("INSERT INTO search VALUES(?,?,?)", (
                            value["key"], json.dumps(names, ensure_ascii=False), description))
                    validate_metadata(metadata)
                    source.seek(0)
                    digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        if self._closed:
                            raise ValueError("import cancelled")
                        digest.update(chunk)
                    if stamp != signature(os.fstat(source.fileno())):
                        raise ValueError("source changed during import")
                    metadata["sha256"] = digest.hexdigest()
                    metadata["index_version"] = INDEX_VERSION
                    connection.execute("INSERT INTO metadata VALUES('snapshot',?)", (json.dumps(metadata, ensure_ascii=False),))
                    connection.commit()
                    connection.close()
                    connection = None
                filename = hashlib.sha256((metadata["sha256"] + ':' + str(INDEX_VERSION)).encode()).hexdigest() + '.sqlite3'
                database = self.cache / filename
                if database.exists():
                    temporary.unlink()
                else:
                    os.replace(temporary, database)
                temporary = None
                if self._closed:
                    raise ValueError("import cancelled")
                pointer = {"database": filename, "signature": stamp, "index_version": INDEX_VERSION}
                handle, name = tempfile.mkstemp(prefix="pointer-", dir=self.cache)
                temporary = Path(name)
                with os.fdopen(handle, "w", encoding="utf-8") as output:
                    json.dump(pointer, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.cache / "current.json")
                temporary = None
                with self._lock:
                    self._pointer = pointer
                    self._view = KnowledgeView(database, metadata)
                    self.last_error = None
                return True
            except (OSError, ValueError, TypeError, RecursionError, sqlite3.Error, ijson.JSONError):
                self.last_error = "knowledge_import_failed"
                return False
            finally:
                if connection is not None:
                    connection.close()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
