"""Input normalization and PDF acquisition for the research pipeline."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import quote, unquote, urlparse

from src.artifacts import ArtifactManager
from src.research_models import InputKind, InputSpec, JobState, PaperMetadata


_ARXIV_ID = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?$", re.IGNORECASE)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_ZOTERO_KEY = re.compile(r"^[A-Z0-9]{8}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class HttpClient(Protocol):
    def get(self, url: str) -> HttpResponse: ...


class AcquisitionError(RuntimeError):
    """An input could not be lawfully acquired into a source PDF."""


class AcquisitionInputError(AcquisitionError):
    """The supplied source is missing, inaccessible, or not a usable PDF."""


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    metadata: PaperMetadata
    source_pdf: Path | None
    pdf_sha256: str | None
    state: JobState = JobState.ACQUIRING


class UrlLibHttpClient:
    """Minimal production transport; tests supply a deterministic replacement."""

    def get(self, url: str) -> HttpResponse:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    content=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())
        except urllib.error.URLError as exc:
            raise AcquisitionError(f"HTTP request failed for {url}: {exc.reason}") from exc


def _normalized_arxiv_id(value: str) -> str | None:
    value = value.strip()
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    if not _ARXIV_ID.fullmatch(value):
        return None
    return re.sub(r"v\d+$", "", value, flags=re.IGNORECASE).lower()


def _normalized_doi(value: str) -> str | None:
    value = value.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    value = unquote(value)
    if not _DOI.fullmatch(value):
        return None
    return value.lower()


def parse_input(source: str, *, attachment_key: str | None = None,
                research_interest: str | None = None) -> InputSpec:
    """Classify a user-provided paper reference into a normalized ``InputSpec``."""
    value = source.strip()
    if not value:
        raise ValueError("Unsupported input: empty value")

    local_path = Path(value)
    if local_path.is_file() and local_path.suffix.lower() == ".pdf":
        return InputSpec(InputKind.LOCAL_PDF, str(local_path.resolve()), attachment_key, research_interest)

    item_uri = re.fullmatch(r"zotero://select/library/items/([A-Z0-9]{8})", value, re.IGNORECASE)
    if item_uri:
        return InputSpec(InputKind.ZOTERO_ITEM, item_uri.group(1).upper(), attachment_key, research_interest)
    collection_uri = re.fullmatch(r"zotero://select/library/collections/([A-Z0-9]{8})", value, re.IGNORECASE)
    if collection_uri:
        return InputSpec(InputKind.ZOTERO_COLLECTION, collection_uri.group(1).upper(), attachment_key, research_interest)
    collection = re.fullmatch(r"(?:zotero-)?collection:([A-Z0-9]{8})", value, re.IGNORECASE)
    if collection:
        return InputSpec(InputKind.ZOTERO_COLLECTION, collection.group(1).upper(), attachment_key, research_interest)

    arxiv_id = _normalized_arxiv_id(value)
    if arxiv_id:
        return InputSpec(InputKind.ARXIV, arxiv_id, attachment_key, research_interest)
    doi = _normalized_doi(value)
    if doi:
        return InputSpec(InputKind.DOI, doi, attachment_key, research_interest)

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(".pdf"):
        return InputSpec(InputKind.PDF_URL, value, attachment_key, research_interest)
    if _ZOTERO_KEY.fullmatch(value):
        return InputSpec(InputKind.ZOTERO_ITEM, value.upper(), attachment_key, research_interest)
    raise ValueError(f"Unsupported input: {source}")


class DocumentAcquirer:
    """Acquire non-Zotero inputs using only an injected HTTP transport."""

    def __init__(self, http_client: HttpClient | None = None) -> None:
        self.http_client = http_client or UrlLibHttpClient()

    def acquire(self, spec: InputSpec, artifacts: ArtifactManager) -> AcquisitionResult:
        if spec.kind is InputKind.LOCAL_PDF:
            source = Path(spec.source)
            if not source.is_file():
                raise AcquisitionInputError(f"Local PDF does not exist: {source}")
            try:
                content = source.read_bytes()
            except OSError as exc:
                raise AcquisitionInputError(f"Local PDF is not readable: {source}") from exc
            self._validate_pdf(content, str(source))
            source_pdf = artifacts.copy_source(source)
            return self._result(PaperMetadata(source_url=source.resolve().as_uri()), source_pdf, content)
        if spec.kind is InputKind.PDF_URL:
            content = self._get_pdf(spec.source)
            source_pdf = artifacts.write_source_pdf(content)
            return self._result(PaperMetadata(source_url=spec.source), source_pdf, content)
        if spec.kind is InputKind.ARXIV:
            return self._acquire_arxiv(spec.source, artifacts)
        if spec.kind is InputKind.DOI:
            return self._acquire_doi(spec.source, artifacts)
        raise AcquisitionError("Zotero inputs must be resolved by the Zotero integration")

    def _acquire_arxiv(self, arxiv_id: str, artifacts: ArtifactManager) -> AcquisitionResult:
        feed = self._get(f"https://export.arxiv.org/api/query?id_list={quote(arxiv_id, safe='.')}")
        metadata = self._parse_arxiv(feed, arxiv_id)
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        content = self._get_pdf(pdf_url)
        return self._result(metadata, artifacts.write_source_pdf(content), content)

    def _acquire_doi(self, doi: str, artifacts: ArtifactManager) -> AcquisitionResult:
        message = self._parse_json(self._get(f"https://api.crossref.org/works/{quote(doi, safe='')}"))
        metadata, pdf_url = self._parse_crossref(message, doi)
        if not pdf_url:
            return AcquisitionResult(metadata, None, None, JobState.NEEDS_INPUT)
        try:
            content = self._get_pdf(pdf_url)
        except AcquisitionInputError:
            return AcquisitionResult(metadata, None, None, JobState.NEEDS_INPUT)
        return self._result(metadata, artifacts.write_source_pdf(content), content)

    def _get(self, url: str) -> bytes:
        try:
            response = self.http_client.get(url)
        except AcquisitionError:
            raise
        except Exception as exc:
            raise AcquisitionError(f"HTTP request failed for {url}: {exc}") from exc
        if not 200 <= response.status_code < 300:
            error_type = (
                AcquisitionInputError
                if response.status_code in {400, 401, 403, 404, 410}
                else AcquisitionError
            )
            raise error_type(f"HTTP {response.status_code} while requesting {url}")
        return response.content

    def _get_pdf(self, url: str) -> bytes:
        content = self._get(url)
        self._validate_pdf(content, url)
        return content

    @staticmethod
    def _validate_pdf(content: bytes, source: str) -> None:
        if not content.lstrip().startswith(b"%PDF-"):
            raise AcquisitionInputError(f"Downloaded response from {source} is not a PDF")

    @staticmethod
    def _result(metadata: PaperMetadata, source_pdf: Path, content: bytes) -> AcquisitionResult:
        return AcquisitionResult(metadata, source_pdf, hashlib.sha256(content).hexdigest())

    @staticmethod
    def _parse_json(content: bytes) -> Mapping[str, object]:
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcquisitionError("Crossref returned malformed metadata") from exc
        if not isinstance(value, dict) or not isinstance(value.get("message"), dict):
            raise AcquisitionError("Crossref returned malformed metadata")
        return value["message"]

    @staticmethod
    def _parse_arxiv(content: bytes, arxiv_id: str) -> PaperMetadata:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise AcquisitionError("arXiv returned malformed metadata") from exc
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", namespace)
        if entry is None:
            raise AcquisitionError(f"arXiv did not return metadata for {arxiv_id}")
        title = _collapsed_text(entry.findtext("atom:title", default="", namespaces=namespace))
        published = entry.findtext("atom:published", default="", namespaces=namespace)
        authors = [_collapsed_text(author.findtext("atom:name", default="", namespaces=namespace))
                   for author in entry.findall("atom:author", namespace)]
        return PaperMetadata(
            title=title or None,
            authors=[author for author in authors if author],
            published_date=published[:10] or None,
            arxiv_id=arxiv_id,
            source_url=f"https://arxiv.org/abs/{arxiv_id}",
        )

    @staticmethod
    def _parse_crossref(message: Mapping[str, object], doi: str) -> tuple[PaperMetadata, str | None]:
        title_values = message.get("title")
        title = title_values[0] if isinstance(title_values, list) and title_values and isinstance(title_values[0], str) else None
        authors: list[str] = []
        author_values = message.get("author")
        if isinstance(author_values, list):
            for author in author_values:
                if not isinstance(author, dict):
                    continue
                name = _collapsed_text(" ".join(str(author.get(key, "")) for key in ("given", "family")))
                authors.append(name or str(author.get("name", "")).strip())
        published_date = _crossref_date(message)
        pdf_url: str | None = None
        links = message.get("link")
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and str(link.get("content-type", "")).lower() == "application/pdf":
                    candidate = link.get("URL")
                    if isinstance(candidate, str) and urlparse(candidate).scheme in {"http", "https"}:
                        pdf_url = candidate
                        break
        return PaperMetadata(
            title=title,
            authors=[author for author in authors if author],
            published_date=published_date,
            doi=doi,
            source_url=pdf_url or _string_value(message.get("URL")),
        ), pdf_url


def _collapsed_text(value: str) -> str:
    return " ".join(value.split())


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _crossref_date(message: Mapping[str, object]) -> str | None:
    for field in ("published-print", "published-online", "published", "issued"):
        value = message.get(field)
        if not isinstance(value, dict) or not isinstance(value.get("date-parts"), list):
            continue
        parts = value["date-parts"]
        if not parts or not isinstance(parts[0], list) or not parts[0]:
            continue
        numbers = parts[0]
        if not all(isinstance(number, int) for number in numbers[:3]):
            continue
        return "-".join(f"{number:02d}" if index else f"{number:04d}" for index, number in enumerate(numbers[:3]))
    return None
