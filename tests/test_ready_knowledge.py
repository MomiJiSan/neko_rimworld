"""Publisher directory attachment never scans the complete payload."""
import hashlib
import json
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.knowledge import KnowledgeLibrary
from plugin.plugins.neko_rimworld.agent.wiki_tools import WikiStore
from .test_wiki_tools import entry, write_snapshot


def publish(root, name="Steel", label="钢铁", generation="1"):
    source = write_snapshot(root, [entry(name, label)])
    document = json.loads(source.read_text())
    record = json.dumps(document.pop("entries")[0], ensure_ascii=False).encode()
    body = b'{"entries":[' + record + b'],' + json.dumps(document, ensure_ascii=False).encode()[1:]
    filename = "snapshot-" + generation * 32 + ".json"
    (root / filename).write_bytes(body)
    checksum = hashlib.sha256(body).hexdigest()
    ready = {"format": "neko.defs.index.v1", "data_file": filename, "data_bytes": len(body),
             "data_sha256": checksum, "snapshot": {**document, "sha256": checksum},
             "entries": [{"key": "Verse.ThingDef/" + name, "names": [name, label], "description": "Definition",
                          "offset": len(b'{"entries":['), "length": len(record),
                          "sha256": hashlib.sha256(record).hexdigest()}]}
    (root / "ready.json").write_text(json.dumps(ready, ensure_ascii=False))
    return ready


def test_first_attach_and_query_reads_only_selected_record(tmp_path, monkeypatch):
    ready = publish(tmp_path)
    original = Path.open
    reads = []
    class Reader:
        def __init__(self, inner): self.inner = inner
        def __enter__(self): return self
        def __exit__(self, *args): self.inner.close()
        def __getattr__(self, name): return getattr(self.inner, name)
        def read(self, size=-1):
            assert size == ready["entries"][0]["length"]
            reads.append(size)
            return self.inner.read(size)
    def open_file(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Reader(stream) if path.name == ready["data_file"] else stream
    monkeypatch.setattr(Path, "open", open_file)
    library = KnowledgeLibrary(tmp_path, allow_legacy_import=False)
    assert library.open_view() is not None and not reads
    result = WikiStore(library.open_view()).query("钢铁")
    assert result["entries"][0]["def_name"] == "Steel"
    assert reads == [ready["entries"][0]["length"]]


@pytest.mark.parametrize("mutation", ["offset", "length", "duplicate", "escape", "bad_hash"])
def test_invalid_directory_is_not_replaced_with_slow_legacy_import(tmp_path, mutation):
    ready = publish(tmp_path)
    if mutation == "offset": ready["entries"][0]["offset"] = ready["data_bytes"]
    elif mutation == "length": ready["entries"][0]["length"] = -1
    elif mutation == "duplicate": ready["entries"].append(dict(ready["entries"][0]))
    elif mutation == "escape": ready["data_file"] = "../current.json"
    else: ready["snapshot"]["sha256"] = "0" * 64
    (tmp_path / "ready.json").write_text(json.dumps(ready))
    library = KnowledgeLibrary(tmp_path)
    assert library.open_view() is None
    assert not library.refresh()


def test_changed_payload_is_rejected_and_old_view_is_pinned(tmp_path):
    first = publish(tmp_path)
    library = KnowledgeLibrary(tmp_path, allow_legacy_import=False)
    old = library.open_view()
    publish(tmp_path, "Gold", "黄金", "2")
    assert library.refresh()
    assert old.read_entry("Verse.ThingDef/Steel")["label"] == "钢铁"
    assert library.open_view().read_entry("Verse.ThingDef/Gold")["label"] == "黄金"
    path = tmp_path / first["data_file"]
    path.write_bytes(path.read_bytes().replace(b'Steel', b'Wrong'))
    with pytest.raises(ValueError): old.read_entry("Verse.ThingDef/Steel")


def test_record_hash_is_checked_not_only_directory_metadata(tmp_path):
    ready = publish(tmp_path)
    ready["entries"][0]["sha256"] = "0" * 64
    (tmp_path / "ready.json").write_text(json.dumps(ready))
    view = KnowledgeLibrary(tmp_path, allow_legacy_import=False).open_view()
    with pytest.raises(ValueError, match="checksum"):
        view.read_entry("Verse.ThingDef/Steel")


def test_production_never_runs_legacy_import(tmp_path):
    write_snapshot(tmp_path)
    library = KnowledgeLibrary(tmp_path, allow_legacy_import=False)
    assert not library.refresh() and library.open_view() is None
    assert not (tmp_path / ".index").exists()
    publish(tmp_path)
    assert library.refresh() and library.open_view() is not None
