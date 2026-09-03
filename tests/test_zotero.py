from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

import pytest

from src.research_models import InputKind, InputSpec


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


def test_upsert_non_zotero_creates_parent_attachment_and_uploads_pdf(tmp_path: Path) -> None:
    from src.research_models import PaperMetadata
    from src.zotero import ZoteroClient

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nbytes\n")
    root = "http://localhost:23119/api/"
    items_url = f"{root}users/0/items"
    search = f"{items_url}?q=10.1000%2Fnew&qmode=everything&itemType=-attachment"
    file_url = f"{root}users/0/items/PDF00001/file"
    upload_url = f"{root}local/uploads/upload-1"
    http = RecordingHttpClient({
        ("GET", search): FakeResponse(200, {}, b"[]"),
        ("GET", root): FakeResponse(200, {"Zotero-Server-ID": "server-1"}),
        ("POST", f"{root}local/authorize"): FakeResponse(200, {}, b'{"key":"write-key"}'),
        ("POST", items_url): [
            FakeResponse(200, {}, b'{"successful":{"0":{"key":"PARENT01"}}}'),
            FakeResponse(200, {}, b'{"successful":{"0":{"key":"PDF00001"}}}'),
        ],
        ("POST", file_url): [
            FakeResponse(200, {}, b'{"url":"http://localhost:23119/api/local/uploads/upload-1","uploadKey":"upload-1","contentType":"application/pdf","prefix":"","suffix":""}'),
            FakeResponse(204, {}, b""),
        ],
        ("POST", upload_url): FakeResponse(201, {}, b""),
    })

    result = ZoteroClient(http_client=http).upsert_non_zotero(PaperMetadata(title="New paper", authors=["Ada Lovelace"], doi="10.1000/new"), source)

    assert (result.parent_key, result.attachment_key, result.source_pdf) == ("PARENT01", "PDF00001", source)
    parent_payload = json.loads(http.requests[3][3])
    attachment_payload = json.loads(http.requests[4][3])
    assert parent_payload == [{"itemType": "journalArticle", "title": "New paper", "DOI": "10.1000/new", "creators": [{"creatorType": "author", "name": "Ada Lovelace"}]}]
    assert attachment_payload == [{"itemType": "attachment", "parentItem": "PARENT01", "linkMode": "imported_file", "contentType": "application/pdf", "filename": "source.pdf", "title": "source.pdf"}]
