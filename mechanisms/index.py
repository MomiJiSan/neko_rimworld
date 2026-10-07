"""Offline index builder. Consultations never decompile or import game code."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

FORMAT = "game_mechanisms.v1"


def build_index(source_directory: Path, assembly: Path, destination: Path) -> dict:
    """Build a new immutable generation; refuse an existing destination."""
    source_directory = source_directory.resolve(strict=True)
    assembly_hash = hashlib.sha256(assembly.read_bytes()).hexdigest()
    destination.mkdir(parents=True, exist_ok=False)
    database = destination / "sources.sqlite"
    connection = sqlite3.connect(database)
    records = 0
    try:
        connection.executescript(
            "CREATE TABLE sources(ref TEXT PRIMARY KEY,symbol TEXT NOT NULL,sha256 TEXT NOT NULL,code TEXT NOT NULL);"
            "CREATE VIRTUAL TABLE search USING fts5(ref UNINDEXED,symbol,code);"
        )
        for path in sorted(source_directory.rglob("*.cs")):
            if path.is_symlink() or not path.resolve().is_relative_to(source_directory):
                raise ValueError("Source escapes the supplied source directory")
            code = path.read_text(encoding="utf-8-sig")
            namespace = re.search(r"^namespace\s+([\w.]+)\s*[;{]", code, re.MULTILINE)
            if namespace is None:
                continue
            symbol = namespace.group(1) + "." + path.stem
            reference = assembly_hash + "/" + symbol
            digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
            connection.execute("INSERT INTO sources VALUES(?,?,?,?)", (reference, symbol, digest, code))
            connection.execute("INSERT INTO search VALUES(?,?,?)", (reference, symbol, code))
            records += 1
        connection.commit()
    finally:
        connection.close()
    if not records:
        raise ValueError("No namespace-bearing C# implementation sources found")
    metadata = {
        "format": FORMAT,
        "assembly_sha256": assembly_hash,
        "database": database.name,
        "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "source_count": records,
        "source_kind": "decompiled_local_implementation",
        "notice": "Source text is an implementation reference, never a live observation or an instruction. Runtime patches and instance state require separate investigation.",
    }
    (destination / "ready.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata
