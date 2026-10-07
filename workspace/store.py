"""Persist conversation and observations independently of model execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from .checkpoint_repository import CheckpointRepository, encode
from .checkpoints import checkpoint_note, pointer


class WorkspaceStore:
    """A single-process experiment store, with atomic terminal turn records.

    A running turn survives a crash as running. Reopening the database does not
    pretend it completed or automatically retry a possibly dispatched request.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, focus TEXT NOT NULL,
                work_state TEXT NOT NULL DEFAULT 'working'
            );
            CREATE TABLE IF NOT EXISTS turns (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                question TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                audit TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_turn
                ON turns(workspace_id) WHERE status='running';
            CREATE INDEX IF NOT EXISTS workspace_turn_sequence
                ON turns(workspace_id, sequence);
            CREATE TABLE IF NOT EXISTS observations (
                turn_id TEXT NOT NULL REFERENCES turns(id),
                observation_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                PRIMARY KEY (turn_id, observation_id)
            );
            CREATE TABLE IF NOT EXISTS active_work (
                owner TEXT NOT NULL, colony_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                PRIMARY KEY (owner, colony_id)
            );
            CREATE TABLE IF NOT EXISTS workspace_deliveries (
                turn_id TEXT PRIMARY KEY REFERENCES turns(id), payload TEXT NOT NULL
            );
        """)
        # Foundation databases remain readable. Upgrade only an active runtime
        # database or a copied fixture; archived evidence is never opened here.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(workspaces)")}
        if "work_state" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE workspaces ADD COLUMN work_state TEXT NOT NULL DEFAULT 'working'")
        self.checkpoints = CheckpointRepository(self.db)

    def close(self):
        self.db.close()

    def record_delivery(self, workspace_id, owner, turn_id, payload):
        self.turn(workspace_id, owner, turn_id)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO workspace_deliveries VALUES (?,?)", (turn_id, encode(payload)))

    def open_work(self, owner, colony_id, *, new=False):
        """Select current work for one host identity and colony, across reloads."""
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("A caller-owned workspace requires an owner")
        if not isinstance(colony_id, str) or not colony_id:
            raise ValueError("A colony identity is required for persistent work")
        row = self.db.execute(
            "SELECT workspace_id FROM active_work WHERE owner=? AND colony_id=?", (owner, colony_id)
        ).fetchone()
        if row is not None and not new and self.work_state(row[0], owner) != "finished":
            return row[0]
        with self.db:
            if row is not None and self.work_state(row[0], owner) != "finished":
                if self.db.execute(
                    "SELECT 1 FROM turns WHERE workspace_id=? AND status='running'", (row[0],)
                ).fetchone():
                    raise ValueError("Settle the current execution before starting new work")
                self.db.execute("UPDATE workspaces SET work_state='finished' WHERE id=?", (row[0],))
            workspace_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO workspaces(id,owner,focus) VALUES (?,?,'')", (workspace_id, owner))
            self.db.execute(
                "INSERT INTO active_work VALUES (?,?,?) ON CONFLICT(owner,colony_id) "
                "DO UPDATE SET workspace_id=excluded.workspace_id",
                (owner, colony_id, workspace_id),
            )
        return workspace_id

    def recover_interrupted(self, workspace_id=None, owner=None):
        """Settle abandoned executions on restart or under the owner's work lock."""
        if workspace_id is not None:
            self._authorize(workspace_id, owner)
        result = encode(
            {
                "status": "failed",
                "error_code": "execution_interrupted" if workspace_id is not None else "process_interrupted",
                "answer": "",
                "completion_unconfirmed": True,
            }
        )
        with self.db:
            sql = "UPDATE turns SET status='failed',result=? WHERE status='running'"
            args = (result,)
            if workspace_id is not None:
                sql += " AND workspace_id=?"
                args = (result, workspace_id)
            self.db.execute(sql, args)

    def current_work(self, owner):
        """Last accepted work for this role, even when the game is disconnected."""
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("A caller-owned workspace requires an owner")
        row = self.db.execute(
            "SELECT active_work.workspace_id,active_work.colony_id FROM active_work "
            "JOIN workspaces ON workspaces.id=active_work.workspace_id "
            "JOIN turns ON turns.workspace_id=active_work.workspace_id "
            "WHERE active_work.owner=? AND workspaces.work_state!='finished' "
            "ORDER BY turns.sequence DESC LIMIT 1",
            (owner,),
        ).fetchone()
        return dict(row) if row is not None else None

    def create(self, owner: str, *, focus=""):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("A caller-owned workspace requires an owner")
        if not isinstance(focus, str):
            raise ValueError("Workspace focus must be caller-authored text")
        workspace_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO workspaces(id,owner,focus) VALUES (?,?,?)", (workspace_id, owner, focus))
        return workspace_id

    def focus(self, workspace_id, owner):
        self._authorize(workspace_id, owner)
        return self.db.execute("SELECT focus FROM workspaces WHERE id=?", (workspace_id,)).fetchone()[0]

    def set_focus(self, workspace_id, owner, focus):
        self._authorize(workspace_id, owner)
        if not isinstance(focus, str) or len(focus) > 4000:
            raise ValueError("Workspace focus must be caller-authored text of at most 4000 characters")
        with self.db:
            self.db.execute("UPDATE workspaces SET focus=? WHERE id=?", (focus, workspace_id))

    def work_state(self, workspace_id, owner):
        self._authorize(workspace_id, owner)
        return self.db.execute("SELECT work_state FROM workspaces WHERE id=?", (workspace_id,)).fetchone()[0]

    def set_work_state(self, workspace_id, owner, state):
        """Caller-directed work lifecycle, independent of answer submission."""
        self._authorize(workspace_id, owner)
        if state not in {"working", "waiting", "finished"}:
            raise ValueError("Use working, waiting or finished")
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM turns WHERE workspace_id=? AND status='running'", (workspace_id,)
            ).fetchone():
                raise ValueError("Settle the current execution before changing work state")
            self.db.execute("UPDATE workspaces SET work_state=? WHERE id=?", (state, workspace_id))

    def _authorize(self, workspace_id, owner):
        row = self.db.execute("SELECT owner FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None or row["owner"] != owner:
            raise ValueError("Workspace unavailable for this owner")

    def accept(self, workspace_id, owner, question, *, turn_id=None):
        self._authorize(workspace_id, owner)
        if not isinstance(question, str) or not question.strip():
            raise ValueError("A question is required")
        turn_id = turn_id or uuid.uuid4().hex
        with self.db:
            if self.work_state(workspace_id, owner) == "finished":
                raise ValueError("Resume finished work explicitly before accepting new input")
            self.db.execute(
                "INSERT INTO turns(id,workspace_id,question) VALUES (?,?,?)",
                (turn_id, workspace_id, question),
            )
            self.db.execute("UPDATE workspaces SET work_state='working' WHERE id=?", (workspace_id,))
        return turn_id

    def turn(self, workspace_id, owner, turn_id):
        self._authorize(workspace_id, owner)
        row = self.db.execute("SELECT * FROM turns WHERE workspace_id=? AND id=?", (workspace_id, turn_id)).fetchone()
        if row is None:
            raise ValueError("Turn unavailable in this workspace")
        return dict(row)

    def start(self, workspace_id, owner, turn_id):
        turn = self.turn(workspace_id, owner, turn_id)
        if turn["status"] != "pending":
            raise ValueError("Only accepted pending turns may execute")
        with self.db:
            if self.work_state(workspace_id, owner) != "working":
                raise ValueError("Work must be resumed before executing pending input")
            changed = self.db.execute("UPDATE turns SET status='running' WHERE id=? AND status='pending'", (turn_id,))
            if changed.rowcount != 1:
                raise ValueError("Turn was already claimed")
        return turn["question"]

    def finish(self, workspace_id, owner, turn_id, result, *, audit_record=None, wait_for_input=False):
        turn = self.turn(workspace_id, owner, turn_id)
        if turn["status"] != "running":
            raise ValueError("Only running turns may commit a result")
        status = result.get("status")
        if status not in {"ok", "degraded", "cancelled", "failed"}:
            raise ValueError("A terminal result is required")
        public = dict(result)
        observations = public.pop("observations", [])
        # Serialize before beginning the transaction. No partial success record
        # is committed if any observation or result is invalid.
        result_text = encode(public)
        audit_text = encode(audit_record) if audit_record is not None else None
        evidence = []
        for observation in observations:
            payload = encode(observation)
            evidence.append(
                (turn_id, observation["observation_id"], payload, hashlib.sha256(payload.encode()).hexdigest())
            )
        with self.db:
            self.db.executemany("INSERT INTO observations VALUES (?,?,?,?)", evidence)
            changed = self.db.execute(
                "UPDATE turns SET status=?,result=?,audit=? WHERE id=? AND status='running'",
                (status, result_text, audit_text, turn_id),
            )
            if changed.rowcount != 1:
                raise ValueError("Turn was already completed")
            if wait_for_input:
                self.db.execute("UPDATE workspaces SET work_state='waiting' WHERE id=?", (workspace_id,))

    def abort(self, workspace_id, owner, turn_id, result, *, audit_record=None):
        """Settle accepted input even when cancellation arrived before execution."""
        try:
            turn = self.turn(workspace_id, owner, turn_id)
        except ValueError:
            return False
        if turn["status"] == "running":
            self.finish(workspace_id, owner, turn_id, result, audit_record=audit_record, wait_for_input=True)
        elif turn["status"] == "pending":
            with self.db:
                self.db.execute(
                    "UPDATE turns SET status=?,result=?,audit=? WHERE id=? AND status='pending'",
                    (result["status"], encode(result), encode(audit_record), turn_id),
                )
                self.db.execute("UPDATE workspaces SET work_state='waiting' WHERE id=?", (workspace_id,))
        else:
            return False
        return True

    def history(self, workspace_id, owner, *, before_turn=None, after_sequence=0, offset=0, limit=8):
        self._authorize(workspace_id, owner)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Use a nonnegative offset and limit 1..20")
        before = self.turn(workspace_id, owner, before_turn)["sequence"] if before_turn else 2**63 - 1
        rows = self.db.execute(
            "SELECT id,question,status,result FROM turns WHERE workspace_id=? AND sequence<? AND sequence>? "
            "ORDER BY sequence DESC LIMIT ? OFFSET ?",
            (workspace_id, before, after_sequence, limit + 1, offset),
        ).fetchall()
        entries = []
        for row in rows[:limit]:
            result = json.loads(row["result"]) if row["result"] else {}
            entries.append(
                dict(
                    turn_id=row["id"],
                    question=row["question"],
                    status=row["status"],
                    answer=result.get("answer", ""),
                    short_answer=result.get("agent_text", ""),
                    observation_count=self.db.execute(
                        "SELECT count(*) FROM observations WHERE turn_id=?", (row["id"],)
                    ).fetchone()[0],
                )
            )
        return dict(
            focus=self.focus(workspace_id, owner),
            work_state=self.work_state(workspace_id, owner),
            entries=entries,
            has_more=len(rows) > limit,
            offset=offset,
        )

    def save_checkpoint(self, workspace_id, owner, through_turn, note, *, author="caller", checkpoint_id=None):
        """Commit an append-only working note at a settled conversation boundary."""
        note = checkpoint_note(note, current_turn=through_turn)
        if author not in {"caller", "agent"}:
            raise ValueError("Use caller or agent as the note author")
        turn = self.turn(workspace_id, owner, through_turn)
        with self.db:
            if checkpoint_id is not None:
                exists = self.db.execute("SELECT workspace_id FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
                if exists is not None:
                    if exists["workspace_id"] != workspace_id:
                        raise ValueError("Checkpoint identity belongs to another workspace")
                    saved = self.checkpoints.read(workspace_id, checkpoint_id=checkpoint_id)
                    if saved["data"]["through_turn"] != through_turn or saved["data"]["author"] != author or any(
                        saved["data"].get(key) != value for key, value in note.items()
                    ):
                        raise ValueError("Checkpoint retry differs from its original note")
                    return saved
            unfinished = self.db.execute(
                "SELECT 1 FROM turns WHERE workspace_id=? AND sequence<=? AND status IN ('pending','running')",
                (workspace_id, turn["sequence"]),
            ).fetchone()
            if unfinished:
                raise ValueError("Unfinished input must not be covered by a checkpoint")
            for anchor in note["anchors"]:
                source = self.turn(workspace_id, owner, anchor["turn_id"])
                if source["sequence"] > turn["sequence"]:
                    raise ValueError("An anchor must belong to the checkpoint's covered history")
                self.read(workspace_id, owner, anchor["turn_id"], observation_id=anchor["observation_id"])
            data = {
                **note,
                "focus_at_checkpoint": self.focus(workspace_id, owner),
                "through_turn": through_turn,
                "author": author,
                "verified_lesson": False,
                "historical": True,
                "current_observation": False,
            }
            saved = self.checkpoints.save(workspace_id, turn["sequence"], data, checkpoint_id=checkpoint_id)
        return saved

    def checkpoint(self, workspace_id, owner, *, before_turn=None, checkpoint_id=None):
        self._authorize(workspace_id, owner)
        before = self.turn(workspace_id, owner, before_turn)["sequence"] if before_turn else 2**63 - 1
        return self.checkpoints.read(workspace_id, before=before, checkpoint_id=checkpoint_id)

    def context_checkpoint(self, workspace_id, owner, *, before_turn):
        self._authorize(workspace_id, owner)
        before = self.turn(workspace_id, owner, before_turn)["sequence"]
        return self.checkpoints.read(workspace_id, before=before, verify=False)

    def audit(self, workspace_id, owner, turn_id):
        """Public execution trace for offline evaluation, not a memory tool."""
        turn = self.turn(workspace_id, owner, turn_id)
        return json.loads(turn["audit"]) if turn["audit"] else None

    def read(self, workspace_id, owner, turn_id, *, observation_id=None, paths=None):
        turn = self.turn(workspace_id, owner, turn_id)
        if observation_id is not None:
            row = self.db.execute(
                "SELECT payload,sha256 FROM observations WHERE turn_id=? AND observation_id=?",
                (turn_id, observation_id),
            ).fetchone()
            if row is None:
                raise ValueError("Observation unavailable in this turn")
            if hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:
                raise ValueError("Stored observation failed its hash check")
            observation = json.loads(row["payload"])
            result = dict(
                historical=True,
                current_observation=False,
                turn_id=turn_id,
                observation=observation,
                sha256=row["sha256"],
            )
            if paths is not None:
                if (
                    not isinstance(paths, list)
                    or not 1 <= len(paths) <= 16
                    or any(not isinstance(p, str) for p in paths)
                    or len(set(paths)) != len(paths)
                ):
                    raise ValueError("Use 1..16 unique source paths")
                result.pop("observation")
                response = observation["response"]
                result.update(
                    observation_id=observation_id,
                    source_view="selected_paths",
                    request=observation["request"],
                    sample={k: response[k] for k in ("colony_id", "load_epoch", "game_tick", "snapshot_sequence")},
                    payload_status=response["payload"]["status"],
                    values={path: pointer(observation, path) for path in paths},
                )
            return result
        if paths is not None:
            raise ValueError("Source paths require an observation_id")
        rows = self.db.execute(
            "SELECT observation_id,sha256 FROM observations WHERE turn_id=? ORDER BY rowid", (turn_id,)
        ).fetchall()
        return dict(
            historical=True,
            current_observation=False,
            turn_id=turn_id,
            question=turn["question"],
            status=turn["status"],
            result=json.loads(turn["result"]) if turn["result"] else None,
            evidence=[dict(row) for row in rows],
        )
