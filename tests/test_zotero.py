from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from src.research_models import InputKind, InputSpec, PaperMetadata


@dataclass(frozen=True, slots=True)
class FakeResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes = b""


class RecordingHttpClient:
    def __init__(self, responses: dict[tuple[str, str], FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, Mapping[str, str] | None, bytes | None]] = []

    def request(self, method: str, url: str, *, headers=None, content=None) -> FakeResponse:
        self.requests.append((method, url, headers, content))
        response = self.responses[(method, url)]
        return response.pop(0) if isinstance(response, list) else response


class DurableWriteTransport:
    """Stateful deterministic Zotero wire contract with response-loss injection."""

    root = "http://localhost:23119/api/"

    def __init__(self, *, crash_stage: str) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "zotero" / "write_contract.json")
            .read_text(encoding="utf-8")
        )
        self.authorize_payload = fixture["authorize"]
        self.upload_payload = fixture["upload"]
        self.crash_stage = crash_stage
        self.crashed = False
        self.items: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str, str, Mapping[str, str] | None, bytes | None]] = []
        self.parent_creation_attempts = 0
        self.attachment_creation_attempts = 0
        self.upload_attempts = 0
        self.registration_attempts = 0
        self.uploaded = False
        self.registered = False

    def _maybe_crash(self, stage: str) -> None:
        if self.crash_stage == stage and not self.crashed:
            self.crashed = True
            raise ConnectionResetError(f"lost {stage} response")

    def request(self, method: str, url: str, *, headers=None, content=None) -> FakeResponse:
        self.requests.append((method, url, headers, content))
        parsed = urlparse(url)
        if method == "GET" and url == self.root:
            return FakeResponse(200, {"zotero-server-id": "server-1"})
        if method == "GET" and parsed.path.endswith("/users/0/items"):
            wanted = parse_qs(parsed.query).get("q", [""])[0].casefold()
            matches = [
                item for item in self.items.values()
                if item["data"].get("itemType") != "attachment"
                and wanted in {
                    str(item["data"].get("DOI", "")).casefold(),
                    str(item["data"].get("archiveID", "")).casefold(),
                }
            ]
            return FakeResponse(200, {}, json.dumps(matches).encode())
        item_match = re.search(r"/users/0/items/([A-Z0-9]{8})$", parsed.path)
        if method == "GET" and item_match:
            item = self.items.get(item_match.group(1))
            return FakeResponse(
                200 if item else 404, {}, json.dumps(item).encode() if item else b"",
            )
        if method == "POST" and parsed.path.endswith("/local/authorize"):
            return FakeResponse(200, {}, json.dumps(self.authorize_payload).encode())
        if method == "POST" and parsed.path.endswith("/users/0/items"):
            payload = json.loads(content)[0]
            key = payload["key"]
            item = {"key": key, "data": payload}
            self.items[key] = item
            if payload["itemType"] == "attachment":
                self.attachment_creation_attempts += 1
                self._maybe_crash("attachment")
            else:
                self.parent_creation_attempts += 1
                self._maybe_crash("parent")
            return FakeResponse(
                200, {}, json.dumps({"successful": {"0": {"key": key}}}).encode(),
            )
        file_match = re.search(r"/users/0/items/([A-Z0-9]{8})/file$", parsed.path)
        if method == "POST" and file_match:
            form = parse_qs((content or b"").decode())
            if "upload" in form:
                self.registration_attempts += 1
                self.registered = True
                self._maybe_crash("registration")
                return FakeResponse(204, {})
            if self.registered:
                return FakeResponse(200, {}, b'{"exists":1}')
            return FakeResponse(200, {}, json.dumps(self.upload_payload).encode())
        if method == "POST" and url == self.upload_payload["url"]:
            self.upload_attempts += 1
            self.uploaded = True
            self._maybe_crash("upload")
            return FakeResponse(201, {})
        raise AssertionError(f"Unexpected Zotero request: {method} {url}")


def test_availability_probe_uses_local_api_v3() -> None:
    from src.zotero import ZoteroClient

    base_url = "http://localhost:23119/api/"
    http = RecordingHttpClient({("GET", base_url): FakeResponse(200, {})})

    assert ZoteroClient(http_client=http).is_available() is True
    assert http.requests == [("GET", base_url, {"Zotero-API-Version": "3"}, None)]


def test_resolve_parent_with_one_pdf_child_returns_metadata_and_local_file(tmp_path: Path) -> None:
    from src.zotero import ZoteroClient

    file_path = tmp_path / "paper.pdf"
    file_path.write_bytes(b"%PDF-1.7\n")
    base = "http://localhost:23119/api/users/0/items/"
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper", "DOI": "10.1000/ABC"}}
    attachment = {"key": "PDF00001", "data": {"itemType": "attachment", "contentType": "application/pdf", "parentItem": "PARENT01"}}
    http = RecordingHttpClient({
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PARENT01/children"): FakeResponse(200, {}, json.dumps([attachment]).encode()),
        ("GET", f"{base}PDF00001/file"): FakeResponse(302, {"location": file_path.as_uri()}),
    })

    result = ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))

    assert result.parent_key == "PARENT01"
    assert result.attachment_key == "PDF00001"
    assert result.source_pdf == file_path
    assert result.metadata.title == "A Paper"
    assert result.metadata.doi == "10.1000/abc"
    assert result.metadata.zotero_library_id == "0"
    assert result.zotero_uri == "zotero://select/library/items/PARENT01"


def test_zotero_response_headers_are_case_insensitive(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.zotero import ZoteroClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    http = DurableWriteTransport(crash_stage="")

    result = ZoteroClient(
        http_client=http,
        operation_store=JobStore(tmp_path / "state.sqlite3"),
    ).upsert_non_zotero(
        PaperMetadata(title="Identity-poor paper"),
        source,
    )

    assert result.attachment_key is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Spring 2024", "2024-03"),
        ("2024", "2024"),
        ("2024-05", "2024-05"),
        ("2024-05-17", "2024-05-17"),
        ("0000", None),
        ("Winter 0000", None),
        ("2024-13", None),
    ],
)
def test_zotero_dates_are_normalized_to_notion_safe_iso_precision(
    raw: str, expected: str | None,
) -> None:
    from src.zotero import normalize_zotero_date

    assert normalize_zotero_date(raw) == expected


def test_zotero_metadata_prefers_parsed_date_and_retains_raw_local_value() -> None:
    from src.zotero import _metadata

    item = {
        "key": "PARENT01",
        "data": {
            "itemType": "journalArticle",
            "title": "Dated paper",
            "date": "Spring 2024",
        },
        "meta": {"parsedDate": "2024-04-15"},
    }

    metadata = _metadata(item)

    assert metadata.published_date == "2024-04-15"
    assert metadata.published_date_raw == "Spring 2024"


@pytest.mark.parametrize(
    ("url", "platform", "expected"),
    [
        ("file:///C:/Users/A%20B/paper.pdf", "nt", "C:\\Users\\A B\\paper.pdf"),
        ("file://server/share/paper.pdf", "nt", "\\\\server\\share\\paper.pdf"),
        ("file:///var/lib/paper.pdf", "posix", "/var/lib/paper.pdf"),
    ],
)
def test_file_url_conversion_is_platform_aware(
    url: str, platform: str, expected: str,
) -> None:
    from src.zotero import file_url_to_path

    assert str(file_url_to_path(url, platform=platform)) == expected


def test_resolved_metadata_uses_the_configured_zotero_user_id(tmp_path: Path) -> None:
    from src.config import ZoteroSettings
    from src.zotero import ZoteroClient

    file_path = tmp_path / "paper.pdf"
    file_path.write_bytes(b"%PDF-1.7\n")
    base = "http://localhost:23119/api/users/42/items/"
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper"}}
    attachment = {
        "key": "PDF00001",
        "data": {
            "itemType": "attachment", "contentType": "application/pdf",
            "parentItem": "PARENT01",
        },
    }
    http = RecordingHttpClient({
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PARENT01/children"): FakeResponse(200, {}, json.dumps([attachment]).encode()),
        ("GET", f"{base}PDF00001/file"): FakeResponse(302, {"location": file_path.as_uri()}),
    })

    result = ZoteroClient(
        ZoteroSettings(user_id=42), http_client=http,
    ).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))

    assert result.metadata.zotero_library_id == "42"
    assert result.metadata.canonical_identity() == "zotero:42:PARENT01"


def test_resolve_attachment_key_directly_uses_its_parent_and_file_redirect(tmp_path: Path) -> None:
    from src.zotero import ZoteroClient

    file_path = tmp_path / "linked.pdf"
    file_path.write_bytes(b"%PDF-1.7\n")
    base = "http://localhost:23119/api/users/0/items/"
    attachment = {"key": "PDF00001", "data": {"itemType": "attachment", "contentType": "application/pdf", "parentItem": "PARENT01", "linkMode": "linked_file"}}
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper"}}
    http = RecordingHttpClient({
        ("GET", f"{base}PDF00001"): FakeResponse(200, {}, json.dumps(attachment).encode()),
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PDF00001/file"): FakeResponse(302, {"location": file_path.as_uri()}),
    })

    result = ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PDF00001"))

    assert result.parent_key == "PARENT01"
    assert result.attachment_key == "PDF00001"
    assert result.source_pdf == file_path


def test_explicit_attachment_key_on_a_parent_avoids_ambiguous_child_selection(tmp_path: Path) -> None:
    from src.zotero import ZoteroClient

    file_path = tmp_path / "selected.pdf"
    file_path.write_bytes(b"%PDF-1.7\n")
    base = "http://localhost:23119/api/users/0/items/"
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper"}}
    attachment = {"key": "PDF00002", "data": {"itemType": "attachment", "contentType": "application/pdf", "parentItem": "PARENT01"}}
    http = RecordingHttpClient({
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PDF00002"): FakeResponse(200, {}, json.dumps(attachment).encode()),
        ("GET", f"{base}PDF00002/file"): FakeResponse(302, {"location": file_path.as_uri()}),
    })

    result = ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01", attachment_key="PDF00002"))

    assert result.attachment_key == "PDF00002"
    assert all("/children" not in url for _, url, _, _ in http.requests)


def test_invalid_explicit_attachment_selection_uses_user_correctable_error() -> None:
    from src.zotero import ZoteroClient, ZoteroInputError

    base = "http://localhost:23119/api/users/0/items/"
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle"}}
    attachment = {
        "key": "PDF00002",
        "data": {
            "itemType": "attachment", "contentType": "application/pdf",
            "parentItem": "OTHER001",
        },
    }
    http = RecordingHttpClient({
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PDF00002"): FakeResponse(200, {}, json.dumps(attachment).encode()),
    })

    with pytest.raises(ZoteroInputError, match="not a PDF child"):
        ZoteroClient(http_client=http).resolve(
            InputSpec(InputKind.ZOTERO_ITEM, "PARENT01", attachment_key="PDF00002")
        )


def test_multiple_parent_pdf_candidates_need_explicit_attachment_selection() -> None:
    from src.research_models import JobState
    from src.zotero import ZoteroClient

    base = "http://localhost:23119/api/users/0/items/"
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper"}}
    pdfs = [
        {"key": "PDF00001", "data": {"itemType": "attachment", "contentType": "application/pdf"}},
        {"key": "PDF00002", "data": {"itemType": "attachment", "contentType": "application/pdf"}},
    ]
    http = RecordingHttpClient({
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PARENT01/children"): FakeResponse(200, {}, json.dumps(pdfs).encode()),
    })

    result = ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))

    assert result.state is JobState.NEEDS_INPUT
    assert result.source_pdf is None
    assert result.diagnostic == "Multiple PDF attachments found; select one"
    assert all("/file" not in url for _, url, _, _ in http.requests)


def test_zero_parent_pdf_candidates_need_input() -> None:
    from src.research_models import JobState
    from src.zotero import ZoteroClient

    base = "http://localhost:23119/api/users/0/items/"
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper"}}
    http = RecordingHttpClient({
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PARENT01/children"): FakeResponse(200, {}, b"[]"),
    })

    result = ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))

    assert result.state is JobState.NEEDS_INPUT
    assert result.diagnostic == "No PDF attachment found"


def test_missing_redirected_attachment_file_needs_input_with_diagnostic(tmp_path: Path) -> None:
    from src.research_models import JobState
    from src.zotero import ZoteroClient

    base = "http://localhost:23119/api/users/0/items/"
    attachment = {"key": "PDF00001", "data": {"itemType": "attachment", "contentType": "application/pdf", "parentItem": "PARENT01", "linkMode": "imported_file"}}
    parent = {"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "A Paper"}}
    http = RecordingHttpClient({
        ("GET", f"{base}PDF00001"): FakeResponse(200, {}, json.dumps(attachment).encode()),
        ("GET", f"{base}PARENT01"): FakeResponse(200, {}, json.dumps(parent).encode()),
        ("GET", f"{base}PDF00001/file"): FakeResponse(302, {"location": (tmp_path / "gone.pdf").as_uri()}),
    })

    result = ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PDF00001"))

    assert result.state is JobState.NEEDS_INPUT
    assert result.diagnostic == "Zotero PDF file is missing or inaccessible"


def test_collection_expansion_returns_each_parent_item_key() -> None:
    from src.zotero import ZoteroClient

    url = "http://localhost:23119/api/users/0/collections/COLLECT1/items"
    items = [
        {"key": "PARENT01", "data": {"itemType": "journalArticle"}},
        {"key": "PDF00001", "data": {"itemType": "attachment", "contentType": "application/pdf"}},
    ]
    http = RecordingHttpClient({("GET", url): FakeResponse(200, {}, json.dumps(items).encode())})

    specs = ZoteroClient(http_client=http).expand_collection(InputSpec(InputKind.ZOTERO_COLLECTION, "COLLECT1"))

    assert specs == [InputSpec(InputKind.ZOTERO_ITEM, "PARENT01")]


def test_api_errors_and_unavailable_local_api_are_explicit() -> None:
    from src.zotero import ZoteroClient, ZoteroError

    failing = RecordingHttpClient({("GET", "http://localhost:23119/api/"): FakeResponse(403, {})})
    assert ZoteroClient(http_client=failing).is_available() is False

    unavailable = RecordingHttpClient({})
    assert ZoteroClient(http_client=unavailable).is_available() is False

    http = RecordingHttpClient({("GET", "http://localhost:23119/api/users/0/items/PARENT01"): FakeResponse(500, {})})
    with pytest.raises(ZoteroError, match="HTTP 500"):
        ZoteroClient(http_client=http).resolve(InputSpec(InputKind.ZOTERO_ITEM, "PARENT01"))


def test_upsert_non_zotero_reuses_exact_doi_match_without_writes(tmp_path: Path) -> None:
    from src.research_models import PaperMetadata
    from src.zotero import ZoteroClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    search = "http://localhost:23119/api/users/0/items?q=10.1000%2Fabc&qmode=everything&itemType=-attachment"
    existing = [{"key": "PARENT01", "data": {"itemType": "journalArticle", "title": "Existing", "DOI": "10.1000/abc"}}]
    http = RecordingHttpClient({("GET", search): FakeResponse(200, {}, json.dumps(existing).encode())})

    result = ZoteroClient(http_client=http).upsert_non_zotero(PaperMetadata(doi="10.1000/ABC"), source)

    assert result.parent_key == "PARENT01"
    assert result.attachment_key is None
    assert result.source_pdf == source
    assert all(method == "GET" for method, _, _, _ in http.requests)


def test_durable_upsert_completes_missing_attachment_for_an_existing_parent(
    tmp_path: Path,
) -> None:
    from src.job_store import JobStore
    from src.zotero import ZoteroClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\ndurable fixture\n")
    transport = DurableWriteTransport(crash_stage="never")
    transport.items["PARENT01"] = {
        "key": "PARENT01",
        "data": {
            "key": "PARENT01",
            "itemType": "journalArticle",
            "title": "Existing",
            "DOI": "10.1000/durable-existing",
        },
    }

    result = ZoteroClient(
        http_client=transport,
        operation_store=JobStore(tmp_path / "state.sqlite3"),
    ).upsert_non_zotero(
        PaperMetadata(doi="10.1000/durable-existing"),
        source,
    )

    assert result.parent_key == "PARENT01"
    assert result.attachment_key is not None
    assert transport.parent_creation_attempts == 0
    assert transport.attachment_creation_attempts == 1
    assert transport.registered is True


def test_upsert_non_zotero_creates_parent_attachment_and_uploads_pdf(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import PaperMetadata
    from src.zotero import ZoteroClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nbytes\n")
    http = DurableWriteTransport(crash_stage="")
    http.authorize_payload["remember"] = False

    result = ZoteroClient(
        http_client=http,
        operation_store=JobStore(tmp_path / "state.sqlite3"),
    ).upsert_non_zotero(
        PaperMetadata(
            title="New paper", authors=["Ada Lovelace"], doi="10.1000/new",
        ),
        source,
    )

    assert result.source_pdf == source
    item_writes = [
        request for request in http.requests
        if request[0] == "POST" and request[1].endswith("/users/0/items")
    ]
    parent_payload = json.loads(item_writes[0][3])
    attachment_payload = json.loads(item_writes[1][3])
    assert parent_payload == [{
        "key": result.parent_key,
        "itemType": "journalArticle",
        "title": "New paper",
        "DOI": "10.1000/new",
        "creators": [{"creatorType": "author", "name": "Ada Lovelace"}],
    }]
    assert attachment_payload == [{
        "key": result.attachment_key,
        "itemType": "attachment",
        "parentItem": result.parent_key,
        "linkMode": "imported_file",
        "contentType": "application/pdf",
        "filename": "source.pdf",
        "title": "source.pdf",
    }]
    assert item_writes[0][2]["Zotero-API-Key"] == "fixture-write-key"
    assert item_writes[1][2]["Zotero-API-Key"] == "fixture-write-key"
    assert item_writes[0][2]["Zotero-Write-Token"] != item_writes[1][2]["Zotero-Write-Token"]
    assert len([
        request for request in http.requests
        if request[0] == "POST" and request[1].endswith("/local/authorize")
    ]) == 4


def test_production_transport_preserves_file_redirect_for_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.zotero import UrlLibZoteroHttpClient

    class RedirectingOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": "file:///C:/paper.pdf"}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: RedirectingOpener())
    response = UrlLibZoteroHttpClient().request("GET", "http://localhost:23119/api/users/0/items/PDF00001/file")

    assert response.status_code == 302
    assert response.headers["Location"] == "file:///C:/paper.pdf"


def test_doi_lookup_ignores_search_result_without_any_identifier(tmp_path: Path) -> None:
    from src.job_store import JobStore
    from src.research_models import PaperMetadata
    from src.zotero import ZoteroClient

    source = tmp_path / "source.pdf"; source.write_bytes(b"%PDF-1.7\n")
    http = DurableWriteTransport(crash_stage="")
    http.items["OTHER001"] = {
        "key": "OTHER001",
        "data": {"itemType": "journalArticle", "title": "Unrelated"},
    }

    result = ZoteroClient(
        http_client=http,
        operation_store=JobStore(tmp_path / "state.sqlite3"),
    ).upsert_non_zotero(PaperMetadata(doi="10.1000/new"), source)

    assert result.parent_key != "OTHER001"


def test_unreadable_source_does_not_authorize_or_write(tmp_path: Path) -> None:
    from src.research_models import PaperMetadata
    from src.zotero import ZoteroClient

    http = RecordingHttpClient({})
    with pytest.raises(FileNotFoundError):
        ZoteroClient(http_client=http).upsert_non_zotero(PaperMetadata(title="Missing"), tmp_path / "missing.pdf")
    assert http.requests == []


def test_zotero_new_item_write_fails_closed_without_a_durable_operation_store(
    tmp_path: Path,
) -> None:
    from src.zotero import ZoteroClient, ZoteroError

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    http = RecordingHttpClient({})

    with pytest.raises(ZoteroError, match="durable operation store"):
        ZoteroClient(http_client=http).upsert_non_zotero(
            PaperMetadata(title="Identity-poor paper"), source,
        )

    assert http.requests == []


@pytest.mark.parametrize(
    ("metadata", "crash_stage", "expected_upload_attempts"),
    [
        (PaperMetadata(title="DOI paper", doi="10.1000/durable"), "parent", 1),
        (PaperMetadata(title="arXiv paper", arxiv_id="2401.01234"), "attachment", 1),
        (PaperMetadata(title="Identity-poor paper"), "upload", 2),
        (PaperMetadata(title="Registration paper"), "registration", 1),
    ],
)
def test_durable_zotero_write_resumes_exact_operation_without_duplicate_items(
    tmp_path: Path,
    metadata: PaperMetadata,
    crash_stage: str,
    expected_upload_attempts: int,
) -> None:
    from src.job_store import JobStore
    from src.zotero import ZoteroClient, ZoteroError

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\ndurable fixture\n")
    store = JobStore(tmp_path / "state.sqlite3")
    transport = DurableWriteTransport(crash_stage=crash_stage)

    with pytest.raises(ZoteroError, match="unavailable"):
        ZoteroClient(http_client=transport, operation_store=store).upsert_non_zotero(
            metadata, source,
        )

    result = ZoteroClient(
        http_client=transport, operation_store=store,
    ).upsert_non_zotero(metadata, source)

    assert result.attachment_key is not None
    assert transport.parent_creation_attempts == 1
    assert transport.attachment_creation_attempts == 1
    assert transport.upload_attempts == expected_upload_attempts
    assert transport.registration_attempts == 1
    assert len([
        item for item in transport.items.values()
        if item["data"]["itemType"] != "attachment"
    ]) == 1
    assert len([
        item for item in transport.items.values()
        if item["data"]["itemType"] == "attachment"
    ]) == 1

    import hashlib

    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    operation = store.get_zotero_operation(
        metadata.canonical_identity(source_sha256), source_sha256,
    )
    assert operation is not None
    assert operation["parent_key"] == result.parent_key
    assert operation["attachment_key"] == result.attachment_key
    assert len(operation["parent_write_token"]) == 64
    assert len(operation["attachment_write_token"]) == 64
    events = store.zotero_operation_events(operation["id"])
    assert "parent_creation_planned" in events
    assert {"parent_created", "parent_reconciled"} & set(events)
    assert "attachment_creation_planned" in events
    assert {"attachment_created", "attachment_reconciled"} & set(events)
    assert "upload_authorization_planned" in events
    assert "upload_planned" in events
    assert "registration_planned" in events
    assert events[-1] == "complete"
    assert b"fixture-write-key" not in store.path.read_bytes()
