from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest

from plugin.plugins.neko_rimworld import knowledge
from plugin.plugins.neko_rimworld.knowledge import KnowledgeLibrary


def make_entry(name: str, label: str = "steel", *, description: str = "A definition") -> dict:
    return {
        "key": f"Verse.ThingDef/{name}",
        "type": "Verse.ThingDef",
        "def_name": name,
        "label": label,
        "description": description,
        "source_mod": "核心",
        "package_id": "ludeon.rimworld",
        "facts": [{"name": "stackLimit", "value": "75", "type": "System.Int32"}],
        "related": [],
        "truncated": False,
    }


def write_source(directory: Path, entries: list[dict], *, metadata: dict | None = None) -> Path:
    root = {
        "entries": entries,
        "format": "neko.defs.v1",
        "game_version": "1.6",
        "language": "中文",
        "generated_at": "2026-09-01T00:00:00Z",
        "complete": True,
        "mods": [{"package_id": "ludeon.rimworld", "name": "核心"}],
        "issues": [],
    }
    if metadata:
        root.update(metadata)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "current.json"
    path.write_text(json.dumps(root, ensure_ascii=False), encoding="utf-8")
    return path


def replace_source(directory: Path, entries: list[dict], *, metadata: dict | None = None) -> None:
    replacement = write_source(directory / "replacement", entries, metadata=metadata)
    os.replace(replacement, directory / "current.json")


def test_import_accepts_metadata_after_entries_and_preserves_unicode(tmp_path):
    source_entry = make_entry("钢铁", "钢铁", description="用于建造的材料")
    write_source(tmp_path, [source_entry], metadata={"warnings": ["缺少来源"]})

    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    assert library.refresh() is True
    view = library.open_view()
    assert view is not None
    assert view.metadata["language"] == "中文"
    assert view.metadata["warnings"] == ["缺少来源"]
    assert view.read_entry(source_entry["key"]) == source_entry
    assert view.search_rows() == [
        {
            "key": source_entry["key"],
            "names": ["钢铁"],
            "description": "用于建造的材料",
        }
    ]


def test_duplicate_entry_keys_and_corrupt_source_are_rejected(tmp_path):
    duplicate = make_entry("Steel")
    write_source(tmp_path, [duplicate, dict(duplicate, label="duplicate")])
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    assert library.refresh() is False
    assert library.open_view() is None

    (tmp_path / "current.json").write_text('{"entries": [', encoding="utf-8")
    assert library.refresh() is False
    assert library.open_view() is None


def test_failed_refresh_keeps_old_view_and_successful_refresh_publishes_atomically(tmp_path):
    old_entry = make_entry("Old", "旧")
    new_entry = make_entry("New", "新")
    write_source(tmp_path, [old_entry])
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    assert library.refresh() is True
    old_view = library.open_view()
    assert old_view is not None

    replace_source(tmp_path, [new_entry])
    assert library.refresh() is True
    new_view = library.open_view()
    assert new_view is not None
    assert old_view.metadata["sha256"] != new_view.metadata["sha256"]
    assert old_view.read_entry(old_entry["key"]) == old_entry
    assert old_view.read_entry(new_entry["key"]) is None
    assert new_view.read_entry(new_entry["key"]) == new_entry
    assert new_view.read_entry(old_entry["key"]) is None

    replace_source(tmp_path, [make_entry("Broken")])
    (tmp_path / "current.json").write_text("not json", encoding="utf-8")
    assert library.refresh() is False
    assert library.open_view() is new_view
    assert library.open_view().read_entry(new_entry["key"]) == new_entry


def test_streamed_import_and_unchanged_refresh_do_not_read_whole_source(tmp_path, monkeypatch):
    entries = [make_entry(str(index), description="x" * 8192) for index in range(300)]
    source = write_source(tmp_path, entries)
    source_stat = source.stat()
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    reads = []
    original_fdopen = os.fdopen

    class CountedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            assert 0 <= size < source_stat.st_size, "source must be read in bounded chunks"
            data = self.stream.read(size)
            reads.append(len(data))
            return data

        def readinto(self, buffer):
            assert len(buffer) < source_stat.st_size
            count = self.stream.readinto(buffer)
            reads.append(count)
            return count

    def counted_fdopen(fd, *args, **kwargs):
        stamp = os.fstat(fd)
        stream = original_fdopen(fd, *args, **kwargs)
        if (stamp.st_dev, stamp.st_ino) == (source_stat.st_dev, source_stat.st_ino):
            return CountedStream(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", counted_fdopen)
    assert library.refresh() is True
    assert sum(reads) >= source_stat.st_size
    initial_reads = list(reads)
    assert library.refresh() is True
    assert reads == initial_reads
    view = library.open_view()
    assert view is not None
    assert view.read_entry(entries[-1]["key"]) == entries[-1]
    assert reads == initial_reads


def test_new_process_reuses_cache_without_source_import(tmp_path):
    source_directory = tmp_path / "source"
    cache_directory = tmp_path / "cache"
    entry = make_entry("Steel")
    write_source(source_directory, [entry])
    first = KnowledgeLibrary(source_directory, cache_directory)
    assert first.refresh() is True
    assert first.open_view() is not None
    expected_sha256 = first.open_view().metadata["sha256"]
    first.close()

    (source_directory / "current.json").unlink()
    code = """
import json
import sys
from pathlib import Path
from plugin.plugins.neko_rimworld.knowledge import KnowledgeLibrary
library = KnowledgeLibrary(Path(sys.argv[1]), Path(sys.argv[2]))
view = library.open_view()
assert view is not None
assert library.refresh() is False
assert library.open_view().metadata == view.metadata
print(json.dumps({"entry": view.read_entry("Verse.ThingDef/Steel"),
                  "sha256": view.metadata["sha256"]}))
library.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(source_directory), str(cache_directory)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {"entry": entry, "sha256": expected_sha256}


def test_concurrent_views_use_independent_read_connections(tmp_path):
    entries = [make_entry("Steel", "钢铁"), make_entry("Gold", "黄金")]
    write_source(tmp_path, entries)
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    assert library.refresh() is True
    views = [library.open_view() for _ in range(8)]
    assert all(view is not None for view in views)

    def read(view):
        return view.read_entry("Verse.ThingDef/Steel"), view.search_rows()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(read, views))
    assert all(result[0] == entries[0] for result in results)
    assert all({row["key"] for row in result[1]} == {entry["key"] for entry in entries} for result in results)


def test_background_refresh_imports_source_created_after_first_missing_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "REFRESH_INTERVAL_SECONDS", 0.02)
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    attempts = Queue()
    refresh = library.refresh

    def observed_refresh():
        result = refresh()
        attempts.put((result, threading.current_thread()))
        return result

    monkeypatch.setattr(library, "refresh", observed_refresh)
    worker = None
    try:
        library.request_refresh()
        succeeded, worker = attempts.get(timeout=2)
        assert succeeded is False
        assert library.open_view() is None
        assert worker.daemon

        source_entry = make_entry("Later", "后来出现")
        # Publish the complete file atomically, without another refresh request.
        replace_source(tmp_path, [source_entry])
        for _ in range(20):
            succeeded, running_worker = attempts.get(timeout=2)
            assert running_worker is worker
            if succeeded:
                break
        assert succeeded is True
        view = library.open_view()
        assert view is not None
        assert view.read_entry(source_entry["key"]) == source_entry
    finally:
        library.close()
        if worker is not None:
            worker.join(timeout=2)
            assert not worker.is_alive()


def test_repeated_requests_start_one_daemon_and_close_wakes_interval_wait(tmp_path, monkeypatch):
    # A long interval proves shutdown wakes the worker rather than waiting for it.
    monkeypatch.setattr(knowledge, "REFRESH_INTERVAL_SECONDS", 60)
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    finished_refresh = threading.Event()
    workers = []
    start = threading.Thread.start
    refresh = library.refresh

    def observed_start(thread):
        workers.append(thread)
        return start(thread)

    def observed_refresh():
        result = refresh()
        finished_refresh.set()
        return result

    monkeypatch.setattr(threading.Thread, "start", observed_start)
    monkeypatch.setattr(library, "refresh", observed_refresh)
    try:
        library.request_refresh()
        assert finished_refresh.wait(timeout=2)
        for _ in range(20):
            library.request_refresh()
        assert len(workers) == 1
        assert workers[0].daemon
        assert workers[0].is_alive()
        library.close()
        workers[0].join(timeout=2)
        assert not workers[0].is_alive()
        library.request_refresh()
        assert len(workers) == 1
        assert library.refresh() is False
    finally:
        library.close()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()


def test_concurrent_searches_share_lightweight_directory_for_one_generation(tmp_path):
    source_entry = make_entry("Steel", "钢铁")
    source_entry["facts"] = [{"name": "custom", "value": "large value" * 1000}]
    write_source(tmp_path, [source_entry])
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    assert library.refresh()
    old_view = library.open_view()
    assert old_view is not None
    start = threading.Barrier(8)

    def search(_):
        start.wait(timeout=2)
        view = library.open_view()
        return view.search_rows()

    with ThreadPoolExecutor(max_workers=8) as executor:
        directories = list(executor.map(search, range(8)))
    assert all(rows is directories[0] for rows in directories)
    assert directories[0] == [
        {
            "key": source_entry["key"],
            "names": ["steel", "钢铁"],
            "description": "a definition",
        }
    ]
    assert old_view.read_entry(source_entry["key"]) == source_entry

    replacement = make_entry("Gold", "黄金")
    replace_source(tmp_path, [replacement])
    assert library.refresh()
    new_view = library.open_view()
    assert new_view is not None
    assert [row["key"] for row in new_view.search_rows()] == [replacement["key"]]
    assert old_view.search_rows() is directories[0]
    assert old_view.read_entry(source_entry["key"]) == source_entry
    library.close()


@pytest.mark.parametrize("content", ["", "[]", '{"entries": []}', '{"entries": [], "format": "future"}'])
def test_invalid_source_has_no_view(tmp_path, content):
    (tmp_path / "current.json").write_text(content, encoding="utf-8")
    library = KnowledgeLibrary(tmp_path, tmp_path / "cache")
    assert library.refresh() is False
    assert library.open_view() is None
