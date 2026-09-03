from pathlib import Path
import hashlib

import pytest


PDF_BYTES = b"%PDF-1.7\nsynthetic paper\n%%EOF"


class MappingHttpClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def get(self, url: str):
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize(
    ("source", "kind", "normalized"),
    [
        ("2401.01234v2", "arxiv", "2401.01234"),
        ("arXiv:2401.01234v2", "arxiv", "2401.01234"),
        ("https://arxiv.org/abs/2401.01234v2", "arxiv", "2401.01234"),
        ("https://arxiv.org/pdf/2401.01234v2.pdf", "arxiv", "2401.01234"),
        ("doi:10.1000/ABC.Def", "doi", "10.1000/abc.def"),
        ("https://doi.org/10.1000/ABC.Def", "doi", "10.1000/abc.def"),
        ("https://example.test/download/paper.pdf", "pdf_url", "https://example.test/download/paper.pdf"),
        ("zotero://select/library/items/ABCD1234", "zotero_item", "ABCD1234"),
        ("ABCD1234", "zotero_item", "ABCD1234"),
        ("collection:WXYZ5678", "zotero_collection", "WXYZ5678"),
        ("zotero://select/library/collections/WXYZ5678", "zotero_collection", "WXYZ5678"),
    ],
)
def test_parse_input_recognizes_supported_external_shapes(source: str, kind: str, normalized: str) -> None:
    from src.acquisition import parse_input

    spec = parse_input(source)

    assert spec.kind.value == kind
    assert spec.source == normalized


def test_parse_input_recognizes_an_existing_local_pdf(tmp_path: Path) -> None:
    from src.acquisition import parse_input

    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\n")

    spec = parse_input(str(source))

    assert spec.kind.value == "local_pdf"
    assert spec.source == str(source.resolve())


@pytest.mark.parametrize("source", ["arXiv:not-an-id", "doi:not-a-doi", "notes.txt", "not a source"])
def test_parse_input_rejects_malformed_values(source: str) -> None:
    from src.acquisition import parse_input

    with pytest.raises(ValueError, match="Unsupported input"):
        parse_input(source)


@pytest.mark.parametrize(
    "source",
    [
        "10.5555/example<>[]",
        "https://doi.org/10.5555/example%3C%3E%5B%5D",
    ],
)
def test_parse_input_accepts_standard_doi_suffix_characters(source: str) -> None:
    from src.acquisition import parse_input

    spec = parse_input(source)

    assert spec.kind.value == "doi"
    assert spec.source == "10.5555/example<>[]"


def test_acquire_copies_a_local_pdf_and_computes_its_digest(tmp_path: Path) -> None:
    from src.acquisition import DocumentAcquirer, parse_input
    from src.artifacts import ArtifactManager

    original = tmp_path / "original.pdf"
    original.write_bytes(PDF_BYTES)

    result = DocumentAcquirer().acquire(parse_input(str(original)), ArtifactManager(tmp_path, "local"))

    assert result.source_pdf.read_bytes() == PDF_BYTES
    assert result.pdf_sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert result.metadata.source_url == original.resolve().as_uri()


def test_acquire_downloads_and_validates_a_direct_pdf_url(tmp_path: Path) -> None:
    from src.acquisition import DocumentAcquirer, HttpResponse, parse_input
    from src.artifacts import ArtifactManager

    url = "https://papers.example/paper.pdf"
    client = MappingHttpClient({url: HttpResponse(200, {"content-type": "application/pdf"}, PDF_BYTES)})

    result = DocumentAcquirer(client).acquire(parse_input(url), ArtifactManager(tmp_path, "remote"))

    assert result.metadata.source_url == url
    assert result.source_pdf.read_bytes() == PDF_BYTES
    assert result.pdf_sha256 == hashlib.sha256(PDF_BYTES).hexdigest()


def test_acquire_arxiv_uses_official_metadata_and_pdf_endpoints(tmp_path: Path) -> None:
    from src.acquisition import DocumentAcquirer, HttpResponse, parse_input
    from src.artifacts import ArtifactManager

    arxiv_id = "2401.01234"
    metadata_url = "https://export.arxiv.org/api/query?id_list=2401.01234"
    pdf_url = "https://arxiv.org/pdf/2401.01234.pdf"
    feed = b"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><id>http://arxiv.org/abs/2401.01234v2</id><title> A  paper\n title </title>
      <published>2024-01-02T00:00:00Z</published><author><name>Ada Lovelace</name></author></entry>
    </feed>"""
    client = MappingHttpClient({
        metadata_url: HttpResponse(200, {"content-type": "application/atom+xml"}, feed),
        pdf_url: HttpResponse(200, {"content-type": "application/pdf"}, PDF_BYTES),
    })

    result = DocumentAcquirer(client).acquire(parse_input("arXiv:2401.01234v2"), ArtifactManager(tmp_path, "arxiv"))

    assert result.metadata.title == "A paper title"
    assert result.metadata.authors == ["Ada Lovelace"]
    assert result.metadata.published_date == "2024-01-02"
    assert result.metadata.arxiv_id == arxiv_id
    assert result.metadata.source_url == "https://arxiv.org/abs/2401.01234"
    assert result.source_pdf.read_bytes() == PDF_BYTES


def test_acquire_doi_uses_crossref_pdf_link_without_visiting_the_landing_page(tmp_path: Path) -> None:
    from src.acquisition import DocumentAcquirer, HttpResponse, parse_input
    from src.artifacts import ArtifactManager

    doi = "10.1000/example"
    metadata_url = "https://api.crossref.org/works/10.1000%2Fexample"
    pdf_url = "https://publisher.example/open-paper.pdf"
    message = b'''{"message":{"title":["A Crossref Paper"],"author":[{"given":"Grace","family":"Hopper"}],"published-online":{"date-parts":[[2023,7,3]]},"URL":"https://doi.org/10.1000/example","link":[{"URL":"https://publisher.example/open-paper.pdf","content-type":"application/pdf"}]}}'''
    client = MappingHttpClient({
        metadata_url: HttpResponse(200, {"content-type": "application/json"}, message),
        pdf_url: HttpResponse(200, {"content-type": "application/pdf"}, PDF_BYTES),
    })

    result = DocumentAcquirer(client).acquire(parse_input(doi), ArtifactManager(tmp_path, "doi"))

    assert result.metadata.title == "A Crossref Paper"
    assert result.metadata.authors == ["Grace Hopper"]
    assert result.metadata.published_date == "2023-07-03"
    assert result.metadata.doi == doi
    assert result.metadata.source_url == pdf_url
    assert result.source_pdf.read_bytes() == PDF_BYTES


def test_acquire_doi_without_an_explicit_pdf_link_needs_input(tmp_path: Path) -> None:
    from src.acquisition import DocumentAcquirer, HttpResponse, parse_input
    from src.artifacts import ArtifactManager
    from src.research_models import JobState

    metadata_url = "https://api.crossref.org/works/10.1000%2Fclosed"
    client = MappingHttpClient({
        metadata_url: HttpResponse(200, {"content-type": "application/json"}, b'{"message":{"title":["Closed"]}}'),
    })

    result = DocumentAcquirer(client).acquire(parse_input("10.1000/closed"), ArtifactManager(tmp_path, "closed"))

    assert result.state is JobState.NEEDS_INPUT
    assert result.source_pdf is None
    assert result.metadata.title == "Closed"


@pytest.mark.parametrize(
    ("status_code", "content"),
    [
        (401, PDF_BYTES),
        (403, PDF_BYTES),
        (200, b"<html>paywall</html>"),
    ],
)
def test_acquire_doi_with_an_inaccessible_explicit_pdf_link_needs_input(
    tmp_path: Path, status_code: int, content: bytes,
) -> None:
    from src.acquisition import DocumentAcquirer, HttpResponse, parse_input
    from src.artifacts import ArtifactManager
    from src.research_models import JobState

    metadata_url = "https://api.crossref.org/works/10.1000%2Fclosed"
    pdf_url = "https://publisher.example/closed.pdf"
    client = MappingHttpClient({
        metadata_url: HttpResponse(
            200,
            {"content-type": "application/json"},
            b'{"message":{"title":["Closed"],"link":[{"URL":"https://publisher.example/closed.pdf","content-type":"application/pdf"}]}}',
        ),
        pdf_url: HttpResponse(status_code, {"content-type": "application/pdf"}, content),
    })

    result = DocumentAcquirer(client).acquire(parse_input("10.1000/closed"), ArtifactManager(tmp_path, str(status_code)))

    assert result.state is JobState.NEEDS_INPUT
    assert result.source_pdf is None
    assert result.pdf_sha256 is None


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ("html", "not a PDF"),
        ("failure", "HTTP 502"),
    ],
)
def test_acquire_rejects_invalid_pdf_responses_and_http_failures(
    tmp_path: Path, response: str, error: str,
) -> None:
    from src.acquisition import AcquisitionError, DocumentAcquirer, HttpResponse, parse_input
    from src.artifacts import ArtifactManager

    url = "https://papers.example/paper.pdf"
    payload = b"<html>paywall</html>" if response == "html" else PDF_BYTES
    client = MappingHttpClient({url: HttpResponse(200 if response == "html" else 502, {}, payload)})

    with pytest.raises(AcquisitionError, match=error):
        DocumentAcquirer(client).acquire(parse_input(url), ArtifactManager(tmp_path, response))
