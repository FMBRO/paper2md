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


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY,
                    canonical_identity TEXT NOT NULL UNIQUE,
                    doi TEXT, arxiv_id TEXT, zotero_library_id TEXT,
                    zotero_item_key TEXT, pdf_sha256 TEXT, metadata_json TEXT NOT NULL,
                    artifact_dir TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, paper_id INTEGER, input_json TEXT NOT NULL,
                    state TEXT NOT NULL, artifact_dir TEXT, error TEXT,
                    total_cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, FOREIGN KEY(paper_id) REFERENCES papers(id)
                );
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY, job_id TEXT, request_hash TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL, response_json TEXT NOT NULL, cost_usd REAL NOT NULL,
                    created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_paper_state ON jobs(paper_id, state);
            """)

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
        )

    def create_job(self, input_spec: InputSpec, paper_id: int | None = None,
                   artifact_dir: Path | str | None = None) -> JobRecord:
        job_id, now = str(uuid.uuid4()), self._now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs (id, paper_id, input_json, state, artifact_dir, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, paper_id, self._input_json(input_spec), JobState.QUEUED.value,
                 str(artifact_dir) if artifact_dir else None, now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        return self._record(row)

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
            connection.execute("""INSERT INTO papers
                (canonical_identity, doi, arxiv_id, zotero_library_id, zotero_item_key, pdf_sha256, metadata_json, artifact_dir, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_identity) DO UPDATE SET metadata_json = excluded.metadata_json,
                artifact_dir = COALESCE(excluded.artifact_dir, papers.artifact_dir), updated_at = excluded.updated_at""",
                (identity, metadata.doi, metadata.arxiv_id, metadata.zotero_library_id,
                 metadata.zotero_item_key, pdf_sha256, payload, str(artifact_dir) if artifact_dir else None, now, now))
            return int(connection.execute("SELECT id FROM papers WHERE canonical_identity = ?", (identity,)).fetchone()["id"])

    def find_paper(self, canonical_identity: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute("SELECT * FROM papers WHERE canonical_identity = ?", (canonical_identity,)).fetchone()

    def find_resumable_job(self, paper_id: int) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE paper_id = ? AND state != ? ORDER BY updated_at DESC LIMIT 1",
                                     (paper_id, JobState.COMPLETED.value)).fetchone()
        return self._record(row) if row else None

    def cache_llm_call(self, job_id: str, request_hash: str, model: str,
                       response: Any, cost_usd: float) -> None:
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO llm_calls (job_id, request_hash, model, response_json, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, request_hash, model, json.dumps(response), cost_usd, self._now()),
            )
            if inserted.rowcount:
                connection.execute("UPDATE jobs SET total_cost_usd = total_cost_usd + ?, updated_at = ? WHERE id = ?",
                                   (cost_usd, self._now(), job_id))

    def get_cached_llm_call(self, request_hash: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM llm_calls WHERE request_hash = ?", (request_hash,)).fetchone()
        if row is None:
            return None
        return {"model": row["model"], "response": json.loads(row["response_json"]), "cost_usd": float(row["cost_usd"])}

    def total_cost(self, job_id: str) -> float:
        return self.get_job(job_id).total_cost_usd
