"""SQLite persistence for resumable research-pipeline jobs."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.research_models import InputKind, InputSpec, JobRecord, JobState, PaperMetadata


_ORDER = [
    JobState.QUEUED, JobState.ACQUIRING, JobState.ZOTERO_SYNC,
    JobState.CONVERTING, JobState.QUALITY_CHECK, JobState.EXTRACTING,
    JobState.SYNTHESIZING, JobState.NOTION_SYNC, JobState.COMPLETED,
]
_PAUSED = {JobState.NEEDS_INPUT, JobState.BUDGET_EXCEEDED, JobState.FAILED}


class LeaseOwnershipError(RuntimeError):
    """Raised when a stale worker attempts a fenced persistence operation."""


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY,
                    canonical_identity TEXT NOT NULL UNIQUE,
                    doi TEXT, arxiv_id TEXT, zotero_library_id TEXT,
                    zotero_item_key TEXT, pdf_sha256 TEXT, notion_page_id TEXT,
                    metadata_json TEXT NOT NULL,
                    artifact_dir TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, paper_id INTEGER, input_json TEXT NOT NULL,
                    state TEXT NOT NULL, artifact_dir TEXT, error TEXT,
                    total_cost_usd REAL NOT NULL DEFAULT 0,
                    max_cost_usd REAL NOT NULL DEFAULT 0.50, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, FOREIGN KEY(paper_id) REFERENCES papers(id)
                );
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY, job_id TEXT, request_hash TEXT NOT NULL UNIQUE,
                    cache_key TEXT, model TEXT NOT NULL, provider TEXT,
                    generation_id TEXT,
                    response_json TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}',
                    pricing_json TEXT NOT NULL DEFAULT '{}',
                    cost_usd REAL NOT NULL, cost_resolved INTEGER NOT NULL DEFAULT 1,
                    validated INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_paper_state ON jobs(paper_id, state);
                CREATE TABLE IF NOT EXISTS llm_request_claims (
                    cache_key TEXT PRIMARY KEY, owner_token TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_budget_reservations (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, amount_usd REAL NOT NULL,
                    owner_token TEXT NOT NULL, lease_expires_at REAL NOT NULL,
                    unresolved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS job_checkpoints (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    UNIQUE(job_id, stage),
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
            """)
            self._migrate_papers(connection)
            self._migrate_jobs(connection)
            self._migrate_llm_calls(connection)
            self._migrate_llm_budget_reservations(connection)

    @staticmethod
    def _migrate_jobs(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "max_cost_usd" not in columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN max_cost_usd REAL NOT NULL DEFAULT 0.50"
            )

    @staticmethod
    def _migrate_papers(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(papers)")}
        if "notion_page_id" not in columns:
            connection.execute("ALTER TABLE papers ADD COLUMN notion_page_id TEXT")

    @staticmethod
    def _migrate_llm_calls(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(llm_calls)")
        }
        additions = {
            "cache_key": "TEXT",
            "provider": "TEXT",
            "generation_id": "TEXT",
            "usage_json": "TEXT NOT NULL DEFAULT '{}'",
            "pricing_json": "TEXT NOT NULL DEFAULT '{}'",
            "cost_resolved": "INTEGER NOT NULL DEFAULT 1",
            "validated": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE llm_calls ADD COLUMN {name} {declaration}")
        connection.execute(
            "UPDATE llm_calls SET cache_key = request_hash WHERE cache_key IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_cache_validated "
            "ON llm_calls(cache_key, validated, id)"
        )

    @staticmethod
    def _migrate_llm_budget_reservations(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(llm_budget_reservations)")
        }
        if "owner_token" not in columns:
            connection.execute(
                "ALTER TABLE llm_budget_reservations "
                "ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"
            )
        if "lease_expires_at" not in columns:
            connection.execute(
                "ALTER TABLE llm_budget_reservations "
                "ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _input_json(spec: InputSpec) -> str:
        return json.dumps({"kind": spec.kind.value, "source": spec.source,
                           "attachment_key": spec.attachment_key,
                           "research_interest": spec.research_interest})

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        spec_data = json.loads(row["input_json"])
        return JobRecord(
            id=row["id"], paper_id=row["paper_id"], state=JobState(row["state"]),
            input_spec=InputSpec(kind=InputKind(spec_data["kind"]), source=spec_data["source"],
                                 attachment_key=spec_data.get("attachment_key"),
                                 research_interest=spec_data.get("research_interest")),
            artifact_dir=Path(row["artifact_dir"]) if row["artifact_dir"] else None,
            error=row["error"], total_cost_usd=float(row["total_cost_usd"]),
            max_cost_usd=float(row["max_cost_usd"]),
        )

    def create_job(self, input_spec: InputSpec, paper_id: int | None = None,
                   artifact_dir: Path | str | None = None,
                   max_cost_usd: float = 0.50) -> JobRecord:
        if max_cost_usd < 0:
            raise ValueError("max_cost_usd must not be negative")
        job_id, now = str(uuid.uuid4()), self._now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs (id, paper_id, input_json, state, artifact_dir, max_cost_usd, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, paper_id, self._input_json(input_spec), JobState.QUEUED.value,
                 str(artifact_dir) if artifact_dir else None, max_cost_usd, now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        return self._record(row)

    def link_job_paper(
        self, job_id: str, paper_id: int, artifact_dir: Path | str,
    ) -> JobRecord:
        """Atomically attach the canonical paper and artifact directory to a job."""
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET paper_id = ?, artifact_dir = ?, updated_at = ? WHERE id = ?",
                (paper_id, str(artifact_dir), self._now(), job_id),
            )
        if not updated.rowcount:
            raise KeyError(f"Unknown job: {job_id}")
        return self.get_job(job_id)

    def update_input_spec(
        self,
        job_id: str,
        input_spec: InputSpec,
        *,
        invalidate_from: JobState | None = None,
    ) -> JobRecord:
        """Persist user-supplied resume details such as an attachment selection."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE jobs SET input_json = ?, updated_at = ? WHERE id = ?",
                (self._input_json(input_spec), self._now(), job_id),
            )
            if invalidate_from is not None:
                self._invalidate_checkpoints_in_transaction(
                    connection, job_id, invalidate_from,
                )
        if not updated.rowcount:
            raise KeyError(f"Unknown job: {job_id}")
        return self.get_job(job_id)

    @staticmethod
    def _checkpoint_stages_from(stage: JobState) -> list[str]:
        checkpoint_order = [
            JobState.ACQUIRING, JobState.ZOTERO_SYNC, JobState.CONVERTING,
            JobState.QUALITY_CHECK, JobState.EXTRACTING, JobState.SYNTHESIZING,
            JobState.NOTION_SYNC,
        ]
        if stage not in checkpoint_order:
            raise ValueError(f"{stage.value} is not a checkpoint stage")
        return [value.value for value in checkpoint_order[checkpoint_order.index(stage):]]

    @classmethod
    def _invalidate_checkpoints_in_transaction(
        cls, connection: sqlite3.Connection, job_id: str, stage: JobState,
    ) -> None:
        stages = cls._checkpoint_stages_from(stage)
        placeholders = ", ".join("?" for _ in stages)
        connection.execute(
            f"DELETE FROM job_checkpoints WHERE job_id = ? AND stage IN ({placeholders})",
            (job_id, *stages),
        )

    def invalidate_checkpoints(self, job_id: str, stage: JobState) -> None:
        """Atomically remove a stage checkpoint and every dependent checkpoint."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,),
            ).fetchone() is None:
                raise KeyError(f"Unknown job: {job_id}")
            self._invalidate_checkpoints_in_transaction(connection, job_id, stage)

    def update_job_budget(self, job_id: str, max_cost_usd: float) -> JobRecord:
        if max_cost_usd < 0:
            raise ValueError("max_cost_usd must not be negative")
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET max_cost_usd = ?, updated_at = ? WHERE id = ?",
                (max_cost_usd, self._now(), job_id),
            )
        if not updated.rowcount:
            raise KeyError(f"Unknown job: {job_id}")
        return self.get_job(job_id)

    def reopen_completed_job(self, job_id: str) -> JobRecord:
        """Return a completed job to queued so resume can validate its checkpoints."""
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET state = ?, error = NULL, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    JobState.QUEUED.value, self._now(), job_id,
                    JobState.COMPLETED.value,
                ),
            )
        if not updated.rowcount:
            job = self.get_job(job_id)
            raise ValueError(f"Job is not completed: {job.state.value}")
        return self.get_job(job_id)

    def save_checkpoint(
        self, job_id: str, stage: JobState, payload: Any,
    ) -> None:
        """Durably replace one completed-stage checkpoint in a SQLite transaction."""
        if stage in {JobState.QUEUED, JobState.COMPLETED, *_PAUSED}:
            raise ValueError(f"{stage.value} is not a checkpoint stage")
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,),
            ).fetchone() is None:
                raise KeyError(f"Unknown job: {job_id}")
            connection.execute(
                """INSERT INTO job_checkpoints
                   (job_id, stage, payload_json, completed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(job_id, stage) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       completed_at = excluded.completed_at""",
                (job_id, stage.value, encoded, self._now()),
            )

    def get_checkpoint(self, job_id: str, stage: JobState) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM job_checkpoints WHERE job_id = ? AND stage = ?",
                (job_id, stage.value),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_checkpoints(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT stage, payload_json FROM job_checkpoints
                   WHERE job_id = ? ORDER BY id""",
                (job_id,),
            ).fetchall()
        return {row["stage"]: json.loads(row["payload_json"]) for row in rows}

    def complete_notion_sync(
        self, job_id: str, paper_id: int, page_id: str, payload: Any,
    ) -> None:
        """Persist a returned Notion ID and its checkpoint in one transaction."""
        page_id = page_id.strip()
        if not page_id:
            raise ValueError("Notion page ID must not be empty")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            paper = connection.execute(
                "SELECT 1 FROM papers WHERE id = ?", (paper_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ? AND paper_id = ?", (job_id, paper_id),
            ).fetchone()
            if paper is None or job is None:
                raise KeyError("Unknown job/paper association")
            connection.execute(
                "UPDATE papers SET notion_page_id = ?, updated_at = ? WHERE id = ?",
                (page_id, now, paper_id),
            )
            connection.execute(
                """INSERT INTO job_checkpoints
                   (job_id, stage, payload_json, completed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(job_id, stage) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       completed_at = excluded.completed_at""",
                (
                    job_id, JobState.NOTION_SYNC.value,
                    json.dumps(payload, ensure_ascii=False), now,
                ),
            )

    def transition(self, job_id: str, state: JobState, error: str | None = None) -> JobRecord:
        job = self.get_job(job_id)
        if not self._can_transition(job.state, state):
            raise ValueError(f"Invalid transition: {job.state.value} -> {state.value}")
        with self._connect() as connection:
            connection.execute("UPDATE jobs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                               (state.value, error, self._now(), job_id))
        return self.get_job(job_id)

    @staticmethod
    def _can_transition(current: JobState, target: JobState) -> bool:
        if current is JobState.COMPLETED:
            return False
        if current in _PAUSED:
            return target is JobState.QUEUED
        if target in {JobState.FAILED, JobState.NEEDS_INPUT, JobState.BUDGET_EXCEEDED}:
            return True
        return _ORDER.index(target) == _ORDER.index(current) + 1

    def upsert_paper(self, metadata: PaperMetadata, pdf_sha256: str | None = None,
                     artifact_dir: Path | str | None = None) -> int:
        pdf_sha256 = pdf_sha256.strip().lower() if pdf_sha256 else None
        identity = metadata.canonical_identity(pdf_sha256)
        if identity is None:
            raise ValueError("A paper needs DOI, arXiv ID, Zotero identity, or PDF SHA-256")
        now = self._now()
        payload = json.dumps({"title": metadata.title, "authors": metadata.authors,
                              "published_date": metadata.published_date, "doi": metadata.doi,
                              "arxiv_id": metadata.arxiv_id, "source_url": metadata.source_url,
                              "zotero_library_id": metadata.zotero_library_id,
                              "zotero_item_key": metadata.zotero_item_key})
        with self._connect() as connection:
            rows = self._find_matching_papers(connection, metadata, pdf_sha256, identity)
            if not rows:
                cursor = connection.execute("""INSERT INTO papers
                    (canonical_identity, doi, arxiv_id, zotero_library_id, zotero_item_key, pdf_sha256, metadata_json, artifact_dir, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (identity, metadata.doi, metadata.arxiv_id, metadata.zotero_library_id,
                     metadata.zotero_item_key, pdf_sha256, payload,
                     str(artifact_dir) if artifact_dir else None, now, now))
                return int(cursor.lastrowid)

            primary, *duplicates = rows
            doi = self._first_value(rows, "doi") or metadata.doi
            arxiv_id = self._first_value(rows, "arxiv_id") or metadata.arxiv_id
            zotero_library_id = self._first_value(rows, "zotero_library_id") or metadata.zotero_library_id
            zotero_item_key = self._first_value(rows, "zotero_item_key") or metadata.zotero_item_key
            sha256 = self._first_value(rows, "pdf_sha256") or pdf_sha256
            notion_page_id = self._first_value(rows, "notion_page_id")
            canonical_identity = PaperMetadata(
                doi=doi, arxiv_id=arxiv_id, zotero_library_id=zotero_library_id,
                zotero_item_key=zotero_item_key,
            ).canonical_identity(sha256)
            for duplicate in duplicates:
                connection.execute("UPDATE jobs SET paper_id = ? WHERE paper_id = ?", (primary["id"], duplicate["id"]))
                connection.execute("DELETE FROM papers WHERE id = ?", (duplicate["id"],))
            connection.execute("""UPDATE papers SET canonical_identity = ?, doi = ?, arxiv_id = ?,
                zotero_library_id = ?, zotero_item_key = ?, pdf_sha256 = ?, metadata_json = ?,
                notion_page_id = ?, artifact_dir = COALESCE(?, artifact_dir), updated_at = ? WHERE id = ?""",
                (canonical_identity, doi, arxiv_id, zotero_library_id, zotero_item_key, sha256,
                 payload, notion_page_id, str(artifact_dir) if artifact_dir else None, now, primary["id"]))
            return int(primary["id"])

    @staticmethod
    def _first_value(rows: list[sqlite3.Row], column: str) -> str | None:
        return next((row[column] for row in rows if row[column] is not None), None)

    @staticmethod
    def _find_matching_papers(connection: sqlite3.Connection, metadata: PaperMetadata,
                              pdf_sha256: str | None, identity: str) -> list[sqlite3.Row]:
        clauses, values = ["canonical_identity = ?"], [identity]
        for column, value in (("doi", metadata.doi), ("arxiv_id", metadata.arxiv_id),
                              ("pdf_sha256", pdf_sha256)):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if metadata.zotero_library_id and metadata.zotero_item_key:
            clauses.append("(zotero_library_id = ? AND zotero_item_key = ?)")
            values.extend((metadata.zotero_library_id, metadata.zotero_item_key))
        return connection.execute(
            f"SELECT * FROM papers WHERE {' OR '.join(clauses)} ORDER BY id", values
        ).fetchall()

    def find_paper(self, canonical_identity: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            clauses, values = ["canonical_identity = ?"], [canonical_identity]
            prefix, _, value = canonical_identity.partition(":")
            if prefix == "doi":
                clauses.append("doi = ?")
                values.append(value)
            elif prefix == "arxiv":
                clauses.append("arxiv_id = ?")
                values.append(value)
            elif prefix == "sha256":
                clauses.append("pdf_sha256 = ?")
                values.append(value)
            elif prefix == "zotero":
                library_id, separator, item_key = value.partition(":")
                if separator:
                    clauses.append("(zotero_library_id = ? AND zotero_item_key = ?)")
                    values.extend((library_id, item_key))
            return connection.execute(f"SELECT * FROM papers WHERE {' OR '.join(clauses)}", values).fetchone()

    def get_notion_page_id(self, paper_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT notion_page_id FROM papers WHERE id = ?", (paper_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown paper: {paper_id}")
        return row["notion_page_id"]

    def set_notion_page_id(self, paper_id: int, page_id: str) -> None:
        page_id = page_id.strip()
        if not page_id:
            raise ValueError("Notion page ID must not be empty")
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE papers SET notion_page_id = ?, updated_at = ? WHERE id = ?",
                (page_id, self._now(), paper_id),
            )
        if not updated.rowcount:
            raise KeyError(f"Unknown paper: {paper_id}")

    def find_resumable_job(self, paper_id: int) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE paper_id = ? AND state != ? ORDER BY updated_at DESC LIMIT 1",
                                     (paper_id, JobState.COMPLETED.value)).fetchone()
        return self._record(row) if row else None

    def has_completed_job(self, canonical_identity: str) -> bool:
        """Return whether a canonical paper identity already completed the pipeline."""
        paper = self.find_paper(canonical_identity)
        if paper is None:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE paper_id = ? AND state = ? LIMIT 1",
                (paper["id"], JobState.COMPLETED.value),
            ).fetchone()
        return row is not None

    def cache_llm_call(self, job_id: str, request_hash: str, model: str,
                       response: Any, cost_usd: float) -> None:
        self.record_llm_call(
            job_id,
            request_hash=request_hash,
            cache_key=request_hash,
            model=model,
            provider=None,
            response=response,
            usage={},
            cost_usd=cost_usd,
            validated=True,
        )

    def record_llm_call(
        self,
        job_id: str,
        *,
        request_hash: str,
        cache_key: str,
        model: str,
        provider: str | None,
        response: Any,
        usage: dict[str, Any],
        cost_usd: float,
        validated: bool,
        pricing: dict[str, Any] | None = None,
        generation_id: str | None = None,
        cost_resolved: bool = True,
    ) -> None:
        with self._connect() as connection:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO llm_calls
                   (job_id, request_hash, cache_key, model, provider, generation_id,
                    response_json, usage_json, pricing_json, cost_usd, cost_resolved,
                    validated, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, request_hash, cache_key, model, provider, generation_id,
                 json.dumps(response, ensure_ascii=False),
                 json.dumps(usage, ensure_ascii=False),
                 json.dumps(pricing or {}, ensure_ascii=False),
                 cost_usd, int(cost_resolved), int(validated), self._now()),
            )
            if inserted.rowcount:
                connection.execute("UPDATE jobs SET total_cost_usd = total_cost_usd + ?, updated_at = ? WHERE id = ?",
                                   (cost_usd, self._now(), job_id))

    def record_llm_call_and_settle_budget(
        self,
        job_id: str,
        *,
        request_hash: str,
        cache_key: str,
        model: str,
        provider: str | None,
        response: Any,
        usage: dict[str, Any],
        cost_usd: float,
        validated: bool,
        reservation_id: str,
        request_owner_token: str,
        reservation_owner_token: str,
        authorized_amount_usd: float,
        now: float,
        pricing: dict[str, Any] | None = None,
        generation_id: str | None = None,
        cost_resolved: bool = True,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request_claim = connection.execute(
                """SELECT 1 FROM llm_request_claims
                   WHERE cache_key = ? AND owner_token = ? AND lease_expires_at > ?""",
                (cache_key, request_owner_token, now),
            ).fetchone()
            reservation = connection.execute(
                """SELECT amount_usd FROM llm_budget_reservations
                   WHERE id = ? AND owner_token = ? AND unresolved = 0
                   AND lease_expires_at > ?""",
                (reservation_id, reservation_owner_token, now),
            ).fetchone()
            if request_claim is None or reservation is None:
                raise LeaseOwnershipError("LLM request or budget lease ownership was lost")
            inserted = connection.execute(
                """INSERT OR IGNORE INTO llm_calls
                   (job_id, request_hash, cache_key, model, provider, generation_id,
                    response_json, usage_json, pricing_json, cost_usd, cost_resolved,
                    validated, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, request_hash, cache_key, model, provider, generation_id,
                    json.dumps(response, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    json.dumps(pricing or {}, ensure_ascii=False),
                    cost_usd, int(cost_resolved), int(validated), self._now(),
                ),
            )
            if not inserted.rowcount:
                return False
            connection.execute(
                """UPDATE jobs SET total_cost_usd = total_cost_usd + ?, updated_at = ?
                   WHERE id = ?""",
                (cost_usd, self._now(), job_id),
            )
            if cost_resolved:
                remaining = max(
                    0.0, float(reservation["amount_usd"]) - authorized_amount_usd,
                )
                connection.execute(
                    "UPDATE llm_budget_reservations SET amount_usd = ? WHERE id = ?",
                    (remaining, reservation_id),
                )
            else:
                connection.execute(
                    """UPDATE llm_budget_reservations
                       SET amount_usd = MIN(amount_usd, ?), unresolved = 1
                       WHERE id = ?""",
                    (authorized_amount_usd, reservation_id),
                )
        return True

    def get_cached_llm_result(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM llm_calls WHERE cache_key = ? AND validated = 1 "
                "ORDER BY id DESC LIMIT 1",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_hash": row["request_hash"],
            "model": row["model"],
            "provider": row["provider"],
            "response": json.loads(row["response_json"]),
            "usage": json.loads(row["usage_json"]),
            "cost_usd": float(row["cost_usd"]),
        }

    def llm_attempt_count(self, cache_key: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM llm_calls WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        return int(row["count"])

    def has_unresolved_llm_call(self, cache_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM llm_calls WHERE cache_key = ? AND cost_resolved = 0 LIMIT 1",
                (cache_key,),
            ).fetchone()
        return row is not None

    def claim_llm_request(
        self, cache_key: str, owner_token: str, *, now: float, lease_seconds: float,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM llm_request_claims WHERE cache_key = ? AND lease_expires_at <= ?",
                (cache_key, now),
            )
            inserted = connection.execute(
                """INSERT OR IGNORE INTO llm_request_claims
                   (cache_key, owner_token, lease_expires_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (cache_key, owner_token, now + lease_seconds, self._now()),
            )
        return bool(inserted.rowcount)

    def release_llm_request(self, cache_key: str, owner_token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM llm_request_claims WHERE cache_key = ? AND owner_token = ?",
                (cache_key, owner_token),
            )

    def renew_llm_request(
        self, cache_key: str, owner_token: str, *, now: float, lease_seconds: float,
    ) -> bool:
        with self._connect() as connection:
            renewed = connection.execute(
                """UPDATE llm_request_claims SET lease_expires_at = ?
                   WHERE cache_key = ? AND owner_token = ? AND lease_expires_at > ?""",
                (now + lease_seconds, cache_key, owner_token, now),
            )
        return bool(renewed.rowcount)

    def renew_llm_leases(
        self,
        cache_key: str,
        request_owner_token: str,
        reservation_id: str,
        reservation_owner_token: str,
        *,
        now: float,
        lease_seconds: float,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request_renewed = connection.execute(
                """UPDATE llm_request_claims SET lease_expires_at = ?
                   WHERE cache_key = ? AND owner_token = ? AND lease_expires_at > ?""",
                (now + lease_seconds, cache_key, request_owner_token, now),
            )
            budget_renewed = connection.execute(
                """UPDATE llm_budget_reservations SET lease_expires_at = ?
                   WHERE id = ? AND owner_token = ? AND lease_expires_at > ?""",
                (
                    now + lease_seconds, reservation_id,
                    reservation_owner_token, now,
                ),
            )
            if not request_renewed.rowcount or not budget_renewed.rowcount:
                connection.rollback()
                return False
        return True

    @staticmethod
    def _paper_cost_in_transaction(
        connection: sqlite3.Connection, job_id: str,
    ) -> tuple[float, float]:
        job = connection.execute(
            "SELECT paper_id, total_cost_usd FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        if job["paper_id"] is None:
            actual = float(job["total_cost_usd"])
            reserved = connection.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS total "
                "FROM llm_budget_reservations WHERE job_id = ?",
                (job_id,),
            ).fetchone()["total"]
        else:
            actual = connection.execute(
                "SELECT COALESCE(SUM(total_cost_usd), 0) AS total "
                "FROM jobs WHERE paper_id = ?",
                (job["paper_id"],),
            ).fetchone()["total"]
            reserved = connection.execute(
                """SELECT COALESCE(SUM(r.amount_usd), 0) AS total
                   FROM llm_budget_reservations r
                   JOIN jobs j ON j.id = r.job_id
                   WHERE j.paper_id = ?""",
                (job["paper_id"],),
            ).fetchone()["total"]
        return float(actual), float(reserved)

    def reserve_llm_budget(
        self,
        job_id: str,
        reservation_id: str,
        *,
        owner_token: str,
        amount_usd: float,
        budget_usd: float,
        now: float,
        lease_seconds: float,
    ) -> bool:
        if amount_usd < 0 or budget_usd < 0 or lease_seconds <= 0:
            raise ValueError("budget reservation amounts must not be negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM llm_budget_reservations "
                "WHERE unresolved = 0 AND lease_expires_at <= ?",
                (now,),
            )
            actual, reserved = self._paper_cost_in_transaction(connection, job_id)
            if actual + reserved + amount_usd > budget_usd + 1e-12:
                return False
            inserted = connection.execute(
                """INSERT OR IGNORE INTO llm_budget_reservations
                   (id, job_id, amount_usd, owner_token, lease_expires_at,
                    unresolved, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (
                    reservation_id, job_id, amount_usd, owner_token,
                    now + lease_seconds, self._now(),
                ),
            )
        return bool(inserted.rowcount)

    def renew_llm_budget(
        self, reservation_id: str, owner_token: str, *, now: float, lease_seconds: float,
    ) -> bool:
        with self._connect() as connection:
            renewed = connection.execute(
                """UPDATE llm_budget_reservations SET lease_expires_at = ?
                   WHERE id = ? AND owner_token = ? AND lease_expires_at > ?""",
                (now + lease_seconds, reservation_id, owner_token, now),
            )
        return bool(renewed.rowcount)

    def release_llm_budget(self, reservation_id: str, owner_token: str) -> bool:
        with self._connect() as connection:
            released = connection.execute(
                """DELETE FROM llm_budget_reservations
                   WHERE id = ? AND owner_token = ? AND unresolved = 0""",
                (reservation_id, owner_token),
            )
        return bool(released.rowcount)

    def llm_budget_reservation(self, reservation_id: str) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT amount_usd FROM llm_budget_reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
        return float(row["amount_usd"]) if row else None

    def get_cached_llm_call(self, request_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM llm_calls WHERE request_hash = ?", (request_hash,)).fetchone()
        if row is None:
            return None
        return {"model": row["model"], "response": json.loads(row["response_json"]), "cost_usd": float(row["cost_usd"])}

    def total_cost(self, job_id: str) -> float:
        return self.get_job(job_id).total_cost_usd

    def paper_total_cost(self, job_id: str) -> float:
        job = self.get_job(job_id)
        if job.paper_id is None:
            return job.total_cost_usd
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(total_cost_usd), 0) AS total "
                "FROM jobs WHERE paper_id = ?",
                (job.paper_id,),
            ).fetchone()
        return float(row["total"])
