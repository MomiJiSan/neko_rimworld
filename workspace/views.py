"""Read-only Hosted UI projections of durable work; never execute or recover it."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager


class WorkspaceViews:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def database(self):
        if not self.path.is_file():
            yield None
            return
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    @staticmethod
    def _work(db, workspace_id):
        row = db.execute(
            "SELECT w.id AS workspace_id,w.owner,w.focus,w.work_state,a.colony_id "
            "FROM workspaces w LEFT JOIN active_work a ON a.workspace_id=w.id WHERE w.id=?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise ValueError("This work is not retained")
        return dict(row)

    @staticmethod
    def _turn(db, workspace_id, turn_id):
        row = db.execute("SELECT * FROM turns WHERE workspace_id=? AND id=?", (workspace_id, turn_id)).fetchone()
        if row is None:
            raise ValueError("This exchange is not retained in the selected work")
        return row

    def read(self, action, *, workspace_id="", turn_id="", observation_id="", offset=0, limit=20):
        if any(not isinstance(value, str) or len(value) > 128 for value in (workspace_id, turn_id, observation_id)):
            raise ValueError("Invalid work reference")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid view page")
        if not isinstance(action, str) or action not in {"list", "work", "turn", "observation", "audit"}:
            raise ValueError("Unknown work view")
        with self.database() as db:
            if db is None:
                if action == "list":
                    return {"workspaces": [], "total": 0, "offset": offset, "next_offset": None}
                raise ValueError("No saved work is available")
            if action == "list":
                return self._list(db, offset, limit)
            work = self._work(db, workspace_id)
            if action == "work":
                return self._history(db, work, offset, limit)
            turn = self._turn(db, workspace_id, turn_id)
            if action == "observation":
                row = db.execute(
                    "SELECT payload,sha256 FROM observations WHERE turn_id=? AND observation_id=?",
                    (turn_id, observation_id),
                ).fetchone()
                if row is None or hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:
                    raise ValueError("Original evidence is unavailable or failed its hash check")
                return {
                    "workspace_id": workspace_id,
                    "turn_id": turn_id,
                    "observation_id": observation_id,
                    "historical": True,
                    "current_observation": False,
                    "sha256": row["sha256"],
                    "observation": json.loads(row["payload"]),
                }
            if action == "audit":
                audit = json.loads(turn["audit"]) if turn["audit"] else None
                if audit is None:
                    raise ValueError("No saved audit is available for this exchange")
                events = audit.pop("events", [])
                return {
                    **audit,
                    "workspace_id": workspace_id,
                    "turn_id": turn_id,
                    "events": events[offset : offset + limit],
                    "events_page": {
                        "offset": offset,
                        "limit": limit,
                        "total": len(events),
                        "has_more": offset + limit < len(events),
                    },
                }
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            delivery = (
                db.execute("SELECT payload FROM workspace_deliveries WHERE turn_id=?", (turn_id,)).fetchone()
                if "workspace_deliveries" in tables
                else None
            )
            return {
                "workspace": work,
                "workspace_id": workspace_id,
                "turn_id": turn_id,
                "question": turn["question"],
                "status": turn["status"],
                "result": json.loads(turn["result"]) if turn["result"] else None,
                "handoff": json.loads(delivery[0]) if delivery else None,
                "audit_available": turn["audit"] is not None,
                "evidence": [
                    dict(r)
                    for r in db.execute(
                        "SELECT observation_id,sha256 FROM observations WHERE turn_id=? ORDER BY rowid", (turn_id,)
                    )
                ],
                "historical": True,
                "current_observation": False,
            }

    def _list(self, db, offset, limit):
        rows = db.execute(
            "SELECT w.id AS workspace_id,w.owner,w.work_state,a.colony_id,t.question AS last_question,"
            "t.status AS last_status,t.sequence AS last_sequence,"
            "(SELECT count(*) FROM turns WHERE workspace_id=w.id) AS turn_count "
            "FROM workspaces w LEFT JOIN active_work a ON a.workspace_id=w.id "
            "LEFT JOIN turns t ON t.sequence=(SELECT max(sequence) FROM turns WHERE workspace_id=w.id) "
            "ORDER BY coalesce(t.sequence,0) DESC,w.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = db.execute("SELECT count(*) FROM workspaces").fetchone()[0]
        return {
            "workspaces": [dict(r) for r in rows],
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def _history(self, db, work, offset, limit):
        workspace_id = work["workspace_id"]
        rows = db.execute(
            "SELECT id AS turn_id,sequence,question,status,json_extract(result,'$.agent_text') AS preview,"
            "json_extract(result,'$.report.report_id') AS report_id,"
            "json_extract(result,'$.report.created_at') AS created_at "
            "FROM turns WHERE workspace_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
            (workspace_id, limit, offset),
        ).fetchall()
        total = db.execute("SELECT count(*) FROM turns WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checkpoint = None
        if "checkpoints" in tables:
            row = db.execute(
                "SELECT id,data,data_sha256 FROM checkpoints WHERE workspace_id=? ORDER BY through_sequence DESC,sequence DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
            if row:
                if hashlib.sha256(row["data"].encode()).hexdigest() != row["data_sha256"]:
                    raise ValueError("Saved working note failed its hash check")
                checkpoint = {"checkpoint_id": row["id"], "data": json.loads(row["data"]), "verified_lesson": False}
        return {
            "workspace": work,
            "workspace_id": workspace_id,
            "turns": [dict(r) for r in rows],
            "checkpoint": checkpoint,
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }
