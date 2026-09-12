"""Bounded report artifacts. Explicit retrieval only; never automatic conversation memory."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .agent.audit import audit_safe
from .agent.handoff import validate_full_text
from .state import utc_now

MAX_REPORT_BYTES = 1024 * 1024


class ReportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def plain(value: Any) -> Any:
    if isinstance(value, dict):
        if isinstance(value.get("text_chunks"), list):
            return "".join(value["text_chunks"])
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


class ReportStore:
    def __init__(self, root: Path | None = None, capacity: int = 100):
        if not 1 <= capacity <= 1000:
            raise ValueError("invalid report capacity")
        self.root = root.resolve() if root is not None else None
        self.capacity = capacity
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] | None = {} if root is None else None
        self._memory: dict[str, dict[str, Any]] = {}

    def _path(self, report_id: str) -> Path:
        if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{32}", report_id):
            raise ReportError("invalid_report_id", "Report ID must be the exact 32-character identifier.")
        assert self.root is not None
        path = self.root / (report_id + ".json")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ReportError("invalid_report_path", "Invalid report path.")
        return path

    @staticmethod
    def metadata(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(record.get(key))
            for key in (
                "report_id",
                "state",
                "created_at",
                "question",
                "source_run_id",
                "reference_report_id",
                "evidence_status",
                "observation_count",
                "samples",
            )
        }

    def _load_index(self) -> None:
        if self._index is not None:
            return
        index = {}
        assert self.root is not None
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue
            try:
                record = self._read_file(path.stem)
                index[path.stem] = self.metadata(record)
            except (ReportError, OSError, ValueError):
                continue
        self._index = index

    def _read_file(self, report_id: str) -> dict[str, Any]:
        path = self._path(report_id)
        if not path.is_file():
            raise ReportError("report_not_found", "This report is not retained or has been removed.")
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ReportError("report_invalid", "The saved report exceeds its size limit.")
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or record.get("report_id") != report_id
            or record.get("state") not in {"complete", "draft"}
        ):
            raise ReportError("report_invalid", "The saved report is not valid.")
        record["full_text"] = validate_full_text(record.get("full_text"))
        return record

    def save(self, record: Mapping[str, Any]) -> dict[str, Any]:
        safe = plain(audit_safe(dict(record)))
        safe["full_text"] = validate_full_text(safe.get("full_text"))
        report_id = safe.get("report_id")
        if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{32}", report_id):
            raise ReportError("invalid_report_id", "Invalid report ID.")
        if safe.get("state") not in {"complete", "draft"}:
            raise ReportError("invalid_report_state", "Invalid report state.")
        safe.setdefault("created_at", utc_now())
        safe["observation_count"] = len(safe.get("observations", []))
        safe["samples"] = [
            {
                key: obs.get("response", {}).get(key)
                for key in ("colony_id", "load_epoch", "game_tick", "snapshot_sequence")
            }
            for obs in safe.get("observations", [])
        ]
        safe["evidence_status"] = "retained"
        encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_REPORT_BYTES:
            referenced = {source["observation_id"] for source in safe.get("provenance", {}).get("sources", [])}
            all_observations = safe.get("observations", [])
            safe["observations"] = [obs for obs in all_observations if obs.get("observation_id") in referenced]
            if referenced - {obs.get("observation_id") for obs in safe["observations"]}:
                raise ReportError("report_evidence_missing", "Referenced observations are missing from the report.")
            safe["omitted_observations"] = [
                obs.get("observation_id") for obs in all_observations if obs.get("observation_id") not in referenced
            ]
            safe["evidence_status"] = "referenced_retained" if referenced else "omitted_size_limit"
            encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_REPORT_BYTES:
            raise ReportError("report_too_large", "Report metadata exceeds the storage limit.")
        with self._lock:
            self._load_index()
            assert self._index is not None
            # Report IDs identify immutable terminal artifacts, not a mutable latest-answer slot.
            if report_id in self._index:
                return self.metadata(self.read(report_id))
            if self.root is None:
                self._memory[report_id] = copy.deepcopy(safe)
            else:
                target = self._path(report_id)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.root, prefix=report_id + "-", suffix=".tmp", delete=False
                    ) as file:
                        temporary = Path(file.name)
                        file.write(encoded)
                    os.replace(temporary, target)
                finally:
                    if temporary is not None and temporary.exists():
                        temporary.unlink()
            self._index[report_id] = self.metadata(safe)
            excess = sorted(self._index, key=lambda key: str(self._index[key].get("created_at")))[: -self.capacity]
            for old_id in excess:
                if self.root is None:
                    self._memory.pop(old_id, None)
                else:
                    self._path(old_id).unlink(missing_ok=True)
                self._index.pop(old_id, None)
            return self.metadata(safe)

    def read(self, report_id: str) -> dict[str, Any]:
        if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{32}", report_id):
            raise ReportError("invalid_report_id", "Use an exact report ID from the report list.")
        with self._lock:
            self._load_index()
            if self.root is None:
                record = self._memory.get(report_id)
                if record is None:
                    raise ReportError("report_not_found", "This report is not retained or has been removed.")
                return copy.deepcopy(record)
            return self._read_file(report_id)

    def list(self, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            self._load_index()
            rows = sorted((self._index or {}).values(), key=lambda row: str(row.get("created_at")), reverse=True)
            return {
                "reports": copy.deepcopy(rows[offset : offset + limit]),
                "total": len(rows),
                "offset": offset,
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "capacity": self.capacity,
                "persistent": self.root is not None,
            }

    @staticmethod
    def reference(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **ReportStore.metadata(record),
            "full_text": record["full_text"],
            "evidence_index": [
                {"index": i, "received_at": obs.get("received_at"), "request": obs.get("request")}
                for i, obs in enumerate(record.get("observations", []))
            ],
        }

    @staticmethod
    def markdown(record: Mapping[str, Any]) -> str:
        return (
            "# RimWorld report\n\n"
            + f"Report ID: {record['report_id']}\n\nState: {record['state']}\n\nCreated: {record['created_at']}\n\n"
            + "## Question\n\n"
            + str(record.get("question") or "")
            + "\n\n## Full report\n\n"
            + str(record["full_text"])
            + "\n\n---\nThis is a saved historical report, not a new game observation.\n"
        )
