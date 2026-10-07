"""Persist working handoffs and source digests; online reads do not scan bodies."""

import hashlib
import json
import uuid


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class CheckpointRepository:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                through_sequence INTEGER NOT NULL,
                created_after_sequence INTEGER NOT NULL,
                data TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                data_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS workspace_checkpoint_sequence
                ON checkpoints(workspace_id, through_sequence, sequence);
            CREATE TABLE IF NOT EXISTS checkpoint_sources (
                turn_id TEXT PRIMARY KEY REFERENCES turns(id), digest TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS sealed_turn
                BEFORE UPDATE ON turns WHEN OLD.status NOT IN ('pending','running')
                BEGIN SELECT RAISE(ABORT,'Terminal turn is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS sealed_observation
                BEFORE UPDATE ON observations
                BEGIN SELECT RAISE(ABORT,'Original observation is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS sealed_checkpoint
                BEFORE UPDATE ON checkpoints
                BEGIN SELECT RAISE(ABORT,'Checkpoint is immutable'); END;
        """)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(checkpoints)")}
        if "source_hash_kind" not in columns:
            with db:
                db.execute(
                    "ALTER TABLE checkpoints ADD COLUMN source_hash_kind TEXT NOT NULL DEFAULT 'full_history_v1'"
                )

    def _rows(self, workspace_id, through):
        return self.db.execute(
            "SELECT id,sequence,question,status,result,audit FROM turns WHERE workspace_id=? AND sequence<=? ORDER BY sequence",
            (workspace_id, through),
        )

    def _turn_digest(self, row):
        digest = hashlib.sha256(encode(dict(row)).encode())
        for evidence in self.db.execute(
            "SELECT observation_id,payload,sha256 FROM observations WHERE turn_id=? ORDER BY observation_id",
            (row["id"],),
        ):
            digest.update(encode(dict(evidence)).encode())
        return digest.hexdigest()

    def _full_history_hash(self, workspace_id, through):
        # Legacy checkpoints retain the exact old algorithm and stored hash.
        digest = hashlib.sha256()
        for row in self._rows(workspace_id, through):
            digest.update(encode(dict(row)).encode())
            for evidence in self.db.execute(
                "SELECT observation_id,payload,sha256 FROM observations WHERE turn_id=? ORDER BY observation_id",
                (row["id"],),
            ):
                digest.update(encode(dict(evidence)).encode())
        return digest.hexdigest()

    def source_hash(self, workspace_id, through, *, verify=False):
        if verify:
            for row in self._rows(workspace_id, through):
                stored = self.db.execute(
                    "SELECT digest FROM checkpoint_sources WHERE turn_id=?", (row["id"],)
                ).fetchone()
                if stored is None or stored["digest"] != self._turn_digest(row):
                    raise ValueError("Checkpoint source history changed")
        digest = hashlib.sha256()
        for row in self.db.execute(
            "SELECT turns.id,turns.sequence,checkpoint_sources.digest FROM turns JOIN checkpoint_sources ON turns.id=turn_id "
            "WHERE workspace_id=? AND turns.sequence<=? ORDER BY turns.sequence",
            (workspace_id, through),
        ):
            digest.update(encode(dict(row)).encode())
        return digest.hexdigest()

    def save(self, workspace_id, through, data, *, checkpoint_id=None):
        # Only newly covered terminal sources need their bodies hashed.
        missing = self.db.execute(
            "SELECT turns.id,sequence,question,status,result,audit FROM turns "
            "LEFT JOIN checkpoint_sources ON turns.id=turn_id "
            "WHERE workspace_id=? AND sequence<=? AND checkpoint_sources.turn_id IS NULL ORDER BY sequence",
            (workspace_id, through),
        ).fetchall()
        for row in missing:
            self.db.execute("INSERT INTO checkpoint_sources VALUES (?,?)", (row["id"], self._turn_digest(row)))
        payload = encode(data)
        ident = checkpoint_id or uuid.uuid4().hex
        accepted = self.db.execute("SELECT max(sequence) FROM turns WHERE workspace_id=?", (workspace_id,)).fetchone()[
            0
        ]
        source = self.source_hash(workspace_id, through)
        self.db.execute(
            "INSERT INTO checkpoints(id,workspace_id,through_sequence,created_after_sequence,data,source_sha256,data_sha256,source_hash_kind) VALUES (?,?,?,?,?,?,?,?)",
            (
                ident,
                workspace_id,
                through,
                accepted,
                payload,
                source,
                hashlib.sha256(payload.encode()).hexdigest(),
                "turn_digest_v1",
            ),
        )
        return self.read(workspace_id, checkpoint_id=ident, verify=False)

    def read(self, workspace_id, *, before=2**63 - 1, checkpoint_id=None, verify=True):
        sql = "SELECT * FROM checkpoints WHERE workspace_id=? AND through_sequence<? AND created_after_sequence<?"
        args = [workspace_id, before, before]
        if checkpoint_id is not None:
            sql += " AND id=?"
            args.append(checkpoint_id)
        row = self.db.execute(sql + " ORDER BY through_sequence DESC,sequence DESC LIMIT 1", args).fetchone()
        if row is None:
            if checkpoint_id is not None:
                raise ValueError("Checkpoint unavailable in this history")
            return None
        if hashlib.sha256(row["data"].encode()).hexdigest() != row["data_sha256"]:
            raise ValueError("Checkpoint data failed its hash check")
        actual = (
            self._full_history_hash(workspace_id, row["through_sequence"])
            if row["source_hash_kind"] == "full_history_v1"
            else self.source_hash(workspace_id, row["through_sequence"], verify=verify)
        )
        if actual != row["source_sha256"]:
            raise ValueError("Checkpoint source history changed")
        return {
            "checkpoint_id": row["id"],
            "through_sequence": row["through_sequence"],
            "created_after_sequence": row["created_after_sequence"],
            "source_sha256": row["source_sha256"],
            "data_sha256": row["data_sha256"],
            "data": json.loads(row["data"]),
        }
