from pathlib import Path

import pytest


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
