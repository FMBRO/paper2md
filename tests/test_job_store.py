from pathlib import Path
import sqlite3

import pytest


def test_job_store_persists_notion_page_id_on_the_canonical_paper(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import PaperMetadata

    store = JobStore(tmp_path / "state.sqlite3")
    paper_id = store.upsert_paper(PaperMetadata(doi="10.1000/notion"))

    assert store.get_notion_page_id(paper_id) is None
    store.set_notion_page_id(paper_id, "notion-page-1")

    assert store.get_notion_page_id(paper_id) == "notion-page-1"


def test_job_store_enforces_ordered_state_transitions(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec, JobState

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.DOI, "10.1/example"))
    moved = store.transition(job.id, JobState.ACQUIRING)

    assert moved.state is JobState.ACQUIRING
    with pytest.raises(ValueError, match="Invalid transition"):
        store.transition(job.id, JobState.COMPLETED)


def test_job_store_persists_canonical_paper_identity_and_resumable_job(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec, PaperMetadata

    store = JobStore(tmp_path / "paper2md.sqlite3")
    paper_id = store.upsert_paper(PaperMetadata(doi="DOI:10.5/UPPER"), "a" * 64)
    same_paper_id = store.upsert_paper(PaperMetadata(doi="https://doi.org/10.5/upper"), "b" * 64)
    job = store.create_job(InputSpec(InputKind.DOI, "10.5/upper"), paper_id=paper_id)

    reopened = JobStore(tmp_path / "paper2md.sqlite3")

    assert same_paper_id == paper_id
    assert reopened.find_paper("doi:10.5/upper")["pdf_sha256"] == "a" * 64
    assert reopened.find_resumable_job(paper_id).id == job.id


def test_job_store_returns_cached_llm_call_and_tracks_actual_cost(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    store.cache_llm_call(job.id, "request-hash", "example/model", {"summary": "ok"}, 0.12)

    assert store.get_cached_llm_call("request-hash") == {
        "model": "example/model", "response": {"summary": "ok"}, "cost_usd": 0.12,
    }
    assert store.total_cost(job.id) == 0.12


def test_replacing_a_cached_call_does_not_charge_a_job_twice(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.DOI, "10.9/cached"))
    store.cache_llm_call(job.id, "same-request", "example/model", {"version": 1}, 0.12)
    store.cache_llm_call(job.id, "same-request", "example/model", {"version": 2}, 0.12)

    assert store.total_cost(job.id) == 0.12


def test_a_job_needing_input_can_be_queued_again_for_resume(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec, JobState

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.ZOTERO_ITEM, "ABCD1234"))
    store.transition(job.id, JobState.ACQUIRING)
    store.transition(job.id, JobState.NEEDS_INPUT, "Choose an attachment")

    resumed = store.transition(job.id, JobState.QUEUED)

    assert resumed.state is JobState.QUEUED
    assert resumed.error is None


def test_job_store_rejects_job_for_nonexistent_paper(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")

    with pytest.raises(Exception, match="FOREIGN KEY"):
        store.create_job(InputSpec(InputKind.DOI, "10.1/missing"), paper_id=999)


def test_job_store_rejects_llm_call_for_nonexistent_job(tmp_path: Path) -> None:
    from src.job_store import JobStore

    store = JobStore(tmp_path / "paper2md.sqlite3")

    with pytest.raises(Exception, match="FOREIGN KEY"):
        store.cache_llm_call("missing-job", "request", "example/model", {"ok": True}, 0.01)


def test_job_store_promotes_sha_identity_to_doi_and_merges_identifiers(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import PaperMetadata

    store = JobStore(tmp_path / "paper2md.sqlite3")
    digest = "a" * 64
    original_id = store.upsert_paper(PaperMetadata(title="PDF only"), digest)
    promoted_id = store.upsert_paper(
        PaperMetadata(
            title="Enriched", doi="10.1/Promoted", arxiv_id="2401.12345",
            zotero_library_id="0", zotero_item_key="ABCD1234",
        ),
        digest,
    )

    promoted = store.find_paper("doi:10.1/promoted")
    assert promoted_id == original_id
    assert promoted["id"] == original_id
    assert promoted["canonical_identity"] == "doi:10.1/promoted"
    assert promoted["arxiv_id"] == "2401.12345"
    assert promoted["zotero_library_id"] == "0"
    assert promoted["zotero_item_key"] == "ABCD1234"
    assert store.find_paper(f"sha256:{digest}")["id"] == original_id


def test_job_store_records_llm_usage_provider_and_only_reuses_validated_result(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    store.record_llm_call(
        job.id,
        request_hash="attempt-1",
        cache_key="semantic-request",
        model="example/model",
        provider="example-provider",
        response={"unvalidated": "bad"},
        usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.01},
        cost_usd=0.01,
        validated=False,
    )

    assert store.get_cached_llm_result("semantic-request") is None

    store.record_llm_call(
        job.id,
        request_hash="attempt-2",
        cache_key="semantic-request",
        model="example/model",
        provider="fallback-provider",
        response={"validated": "good"},
        usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15, "cost": 0.02},
        cost_usd=0.02,
        validated=True,
    )

    cached = store.get_cached_llm_result("semantic-request")
    assert cached == {
        "request_hash": "attempt-2",
        "model": "example/model",
        "provider": "fallback-provider",
        "response": {"validated": "good"},
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15, "cost": 0.02},
        "cost_usd": 0.02,
    }
    assert store.total_cost(job.id) == pytest.approx(0.03)


def test_job_store_migrates_task_one_llm_cache_schema(tmp_path: Path) -> None:
    from src.job_store import JobStore

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY, canonical_identity TEXT NOT NULL UNIQUE,
                doi TEXT, arxiv_id TEXT, zotero_library_id TEXT, zotero_item_key TEXT,
                pdf_sha256 TEXT, metadata_json TEXT NOT NULL, artifact_dir TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, paper_id INTEGER, input_json TEXT NOT NULL,
                state TEXT NOT NULL, artifact_dir TEXT, error TEXT,
                total_cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY, job_id TEXT, request_hash TEXT NOT NULL UNIQUE,
                model TEXT NOT NULL, response_json TEXT NOT NULL, cost_usd REAL NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

    JobStore(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(llm_calls)")}
    assert {"cache_key", "provider", "usage_json", "validated"} <= columns


def test_stale_budget_reservation_is_reclaimed_when_job_resumes(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    assert store.reserve_llm_budget(
        job.id,
        "abandoned",
        owner_token="dead-worker",
        amount_usd=0.4,
        budget_usd=0.5,
        now=100.0,
        lease_seconds=10.0,
    )
    assert not store.reserve_llm_budget(
        job.id,
        "too-early",
        owner_token="resume-worker",
        amount_usd=0.2,
        budget_usd=0.5,
        now=109.0,
        lease_seconds=10.0,
    )

    assert store.reserve_llm_budget(
        job.id,
        "reclaimed",
        owner_token="resume-worker",
        amount_usd=0.2,
        budget_usd=0.5,
        now=111.0,
        lease_seconds=10.0,
    )
    assert store.llm_budget_reservation("abandoned") is None
    assert store.llm_budget_reservation("reclaimed") == pytest.approx(0.2)


def test_atomic_llm_settlement_requires_current_request_and_budget_owners(
    tmp_path: Path,
) -> None:
    from src.job_store import JobStore, LeaseOwnershipError
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    assert store.claim_llm_request(
        "logical", "request-owner", now=100.0, lease_seconds=10.0,
    )
    assert store.reserve_llm_budget(
        job.id,
        "reservation",
        owner_token="budget-owner",
        amount_usd=0.1,
        budget_usd=0.5,
        now=100.0,
        lease_seconds=10.0,
    )
    settlement = {
        "job_id": job.id,
        "request_hash": "attempt",
        "cache_key": "logical",
        "model": "example/model",
        "provider": "provider",
        "response": {"ok": True},
        "usage": {"cost": 0.03},
        "cost_usd": 0.03,
        "validated": True,
        "reservation_id": "reservation",
        "authorized_amount_usd": 0.05,
        "now": 101.0,
    }

    with pytest.raises(LeaseOwnershipError):
        store.record_llm_call_and_settle_budget(
            **settlement,
            request_owner_token="stale-request-owner",
            reservation_owner_token="budget-owner",
        )
    with pytest.raises(LeaseOwnershipError):
        store.record_llm_call_and_settle_budget(
            **settlement,
            request_owner_token="request-owner",
            reservation_owner_token="stale-budget-owner",
        )

    assert store.total_cost(job.id) == 0
    assert store.get_cached_llm_result("logical") is None
    assert store.llm_budget_reservation("reservation") == pytest.approx(0.1)

    store.record_llm_call_and_settle_budget(
        **settlement,
        request_owner_token="request-owner",
        reservation_owner_token="budget-owner",
    )
    assert store.total_cost(job.id) == pytest.approx(0.03)
    assert store.get_cached_llm_result("logical")["response"] == {"ok": True}
    assert store.llm_budget_reservation("reservation") == pytest.approx(0.05)


def test_budget_reservation_renewal_and_release_are_owner_fenced(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import InputKind, InputSpec

    store = JobStore(tmp_path / "paper2md.sqlite3")
    job = store.create_job(InputSpec(InputKind.ARXIV, "2401.00001"))
    assert store.reserve_llm_budget(
        job.id,
        "reservation",
        owner_token="owner",
        amount_usd=0.4,
        budget_usd=0.5,
        now=100.0,
        lease_seconds=10.0,
    )

    assert not store.renew_llm_budget(
        "reservation", "stale-owner", now=105.0, lease_seconds=10.0,
    )
    assert store.renew_llm_budget(
        "reservation", "owner", now=105.0, lease_seconds=10.0,
    )
    assert not store.release_llm_budget("reservation", "stale-owner")
    assert not store.reserve_llm_budget(
        job.id,
        "blocked",
        owner_token="other",
        amount_usd=0.2,
        budget_usd=0.5,
        now=111.0,
        lease_seconds=10.0,
    )
    assert store.release_llm_budget("reservation", "owner")
