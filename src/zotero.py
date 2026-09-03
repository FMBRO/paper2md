"""Zotero local API access, isolated behind an injectable HTTP client."""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import unquote, urlencode, urlparse

from src.config import ZoteroSettings
from src.research_models import InputKind, InputSpec, JobState, PaperMetadata


@dataclass(frozen=True, slots=True)
class ZoteroHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes = b""


class ZoteroHttpClient(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> ZoteroHttpResponse: ...


class UrlLibZoteroHttpClient:
    """Small production transport; ``httpx.Client`` is also accepted by the client."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> ZoteroHttpResponse:
        request = urllib.request.Request(url, data=content, headers=dict(headers or {}), method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return ZoteroHttpResponse(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            return ZoteroHttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())


class ZoteroClient:
    """Client for a personal Zotero local API library (API version 3)."""

    def __init__(self, settings: ZoteroSettings | None = None, *, http_client: ZoteroHttpClient | None = None) -> None:
        settings = settings or ZoteroSettings()
        self.base_url = settings.base_url.rstrip("/") + "/"
        self.user_id = settings.user_id
        self.http_client = http_client or UrlLibZoteroHttpClient()

    def is_available(self) -> bool:
        try:
            response = self.http_client.request("GET", self.base_url, headers={"Zotero-API-Version": "3"})
        except Exception:
            return False
        return 200 <= response.status_code < 300

    def resolve(self, spec: InputSpec) -> "ZoteroResolution":
        if spec.kind is not InputKind.ZOTERO_ITEM:
            raise ValueError("Only Zotero item inputs can be resolved")
        item = self._get_json(f"users/{self.user_id}/items/{spec.source}")
        data = _item_data(item)
        if data.get("itemType") == "attachment":
            return self._resolve_attachment(item)
        parent_key = _item_key(item)
        if spec.attachment_key:
            attachment = self._get_json(f"users/{self.user_id}/items/{spec.attachment_key}")
            if not _is_pdf_attachment(attachment) or _item_data(attachment).get("parentItem") != parent_key:
                raise ZoteroError("Selected attachment is not a PDF child of the requested Zotero item")
            return self._resolve_attachment(attachment, parent=item)
        children = self._get_json(f"users/{self.user_id}/items/{parent_key}/children")
        attachments = [child for child in _item_list(children) if _is_pdf_attachment(child)]
        if len(attachments) != 1:
            return self._needs_input(parent_key, _metadata(item), None, "No PDF attachment found" if not attachments else "Multiple PDF attachments found; select one")
        attachment_key = _item_key(attachments[0])
        path = self._attachment_path(attachment_key)
        if path is None:
            return self._needs_input(parent_key, _metadata(item), attachment_key, "Zotero PDF file is missing or inaccessible")
        return ZoteroResolution(_metadata(item), path, parent_key, attachment_key)

    def expand_collection(self, spec: InputSpec) -> list[InputSpec]:
        if spec.kind is not InputKind.ZOTERO_COLLECTION:
            raise ValueError("Only Zotero collection inputs can be expanded")
        items = self._get_json(f"users/{self.user_id}/collections/{spec.source}/items")
        return [InputSpec(InputKind.ZOTERO_ITEM, _item_key(item), research_interest=spec.research_interest)
                for item in _item_list(items) if _item_data(item).get("itemType") != "attachment"]

    def upsert_non_zotero(self, metadata: PaperMetadata, source_pdf: Path | str) -> "ZoteroResolution":
        if metadata.zotero_item_key or metadata.zotero_library_id:
            raise ValueError("Existing Zotero-derived items are read-only")
        source = Path(source_pdf)
        identity = metadata.doi or metadata.arxiv_id
        if identity:
            query = urlencode({"q": identity, "qmode": "everything", "itemType": "-attachment"})
            for item in _item_list(self._get_json(f"users/{self.user_id}/items?{query}")):
                data = _item_data(item)
                candidate = PaperMetadata(doi=_as_string(data.get("DOI")), arxiv_id=_as_string(data.get("archiveID")))
                if candidate.doi == metadata.doi or candidate.arxiv_id == metadata.arxiv_id:
                    return ZoteroResolution(_metadata(item), source, _item_key(item), None)
        probe = self._request("GET", "")
        server_id = probe.headers.get("Zotero-Server-ID")
        if not server_id:
            raise ZoteroError("Zotero local API did not provide Zotero-Server-ID required for writes")
        auth = _json_response(self._request("POST", "local/authorize", content=b'{"appName":"paper2md"}', headers={"Content-Type": "application/json", "Zotero-Server-ID": server_id}), "write authorization")
        key = auth.get("key") if isinstance(auth, dict) else None
        if not isinstance(key, str):
            raise ZoteroError("Zotero write authorization was denied")
        def write(path: str, content: bytes, headers: Mapping[str, str]) -> ZoteroHttpResponse:
            response = self._request("POST", path, content=content, headers={"Zotero-Server-ID": server_id, "Zotero-API-Key": key, **headers})
            if not 200 <= response.status_code < 300:
                raise ZoteroError(f"Zotero API returned HTTP {response.status_code} for {path}")
            return response
        parent = {"itemType": "journalArticle", "title": metadata.title or "Untitled"}
        if metadata.doi: parent["DOI"] = metadata.doi
        if metadata.authors: parent["creators"] = [{"creatorType": "author", "name": author} for author in metadata.authors]
        parent_key = _created_key(_json_response(write(f"users/{self.user_id}/items", json.dumps([parent]).encode(), {"Content-Type": "application/json"}), "parent creation"))
        attachment = {"itemType": "attachment", "parentItem": parent_key, "linkMode": "imported_file", "contentType": "application/pdf", "filename": source.name, "title": source.name}
        attachment_key = _created_key(_json_response(write(f"users/{self.user_id}/items", json.dumps([attachment]).encode(), {"Content-Type": "application/json"}), "attachment creation"))
        data = source.read_bytes(); file_path = f"users/{self.user_id}/items/{attachment_key}/file"
        form = urlencode({"md5": hashlib.md5(data).hexdigest(), "filename": source.name, "filesize": len(data), "mtime": int(source.stat().st_mtime * 1000)}).encode()
        upload = _json_response(write(file_path, form, {"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}), "file upload authorization")
        if not isinstance(upload, dict): raise ZoteroError("Zotero file upload authorization returned malformed JSON")
        if upload.get("exists") != 1:
            url, upload_key = upload.get("url"), upload.get("uploadKey")
            if not isinstance(url, str) or not isinstance(upload_key, str): raise ZoteroError("Zotero file upload authorization returned malformed JSON")
            posted = self.http_client.request("POST", url, headers={"Content-Type": str(upload.get("contentType") or "application/pdf"), "Zotero-API-Version": "3"}, content=str(upload.get("prefix", "")).encode() + data + str(upload.get("suffix", "")).encode())
            if posted.status_code != 201: raise ZoteroError(f"Zotero file upload failed with HTTP {posted.status_code}")
            registered = write(file_path, urlencode({"upload": upload_key}).encode(), {"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
            if registered.status_code != 204: raise ZoteroError(f"Zotero file upload registration failed with HTTP {registered.status_code}")
        created = PaperMetadata(title=metadata.title, authors=list(metadata.authors), doi=metadata.doi, arxiv_id=metadata.arxiv_id, source_url=metadata.source_url, zotero_library_id=str(self.user_id), zotero_item_key=parent_key)
        return ZoteroResolution(created, source, parent_key, attachment_key)

    def _resolve_attachment(self, attachment: object, *, parent: object | None = None) -> "ZoteroResolution":
        if not _is_pdf_attachment(attachment):
            raise ZoteroError("Requested Zotero attachment is not a PDF")
        attachment_key = _item_key(attachment)
        parent_key = _item_data(attachment).get("parentItem")
        if not isinstance(parent_key, str) or not parent_key:
            parent_key = attachment_key
        if parent is None and parent_key != attachment_key:
            parent = self._get_json(f"users/{self.user_id}/items/{parent_key}")
        metadata = _metadata(parent or attachment)
        path = self._attachment_path(attachment_key)
        if path is None:
            return self._needs_input(parent_key, metadata, attachment_key, "Zotero PDF file is missing or inaccessible")
        return ZoteroResolution(metadata, path, parent_key, attachment_key)

    def _get_json(self, path: str) -> object:
        response = self._request("GET", path)
        if not 200 <= response.status_code < 300:
            raise ZoteroError(f"Zotero API returned HTTP {response.status_code} for {path}")
        try:
            return json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ZoteroError(f"Zotero API returned malformed JSON for {path}") from exc

    def _request(self, method: str, path: str, *, content: bytes | None = None,
                 headers: Mapping[str, str] | None = None) -> ZoteroHttpResponse:
        request_headers = {"Zotero-API-Version": "3", **(headers or {})}
        try:
            return self.http_client.request(method, self.base_url + path, headers=request_headers, content=content)
        except Exception as exc:
            raise ZoteroError(f"Zotero local API is unavailable: {exc}") from exc

    def _attachment_path(self, attachment_key: str) -> Path | None:
        response = self._request("GET", f"users/{self.user_id}/items/{attachment_key}/file")
        if response.status_code != 302:
            raise ZoteroError(f"Zotero API returned HTTP {response.status_code} for attachment file {attachment_key}")
        location = response.headers.get("location") or response.headers.get("Location")
        if not location or urlparse(location).scheme != "file":
            raise ZoteroError(f"Zotero attachment {attachment_key} did not return a local file redirect")
        path = Path(unquote(urlparse(location).path).lstrip("/"))
        return path if path.is_file() else None

    @staticmethod
    def _needs_input(parent_key: str, metadata: PaperMetadata, attachment_key: str | None,
                     diagnostic: str) -> "ZoteroResolution":
        return ZoteroResolution(metadata, None, parent_key, attachment_key, JobState.NEEDS_INPUT, diagnostic)


class ZoteroError(RuntimeError):
    """The local Zotero API could not fulfill an unambiguous request."""


@dataclass(frozen=True, slots=True)
class ZoteroResolution:
    metadata: PaperMetadata
    source_pdf: Path | None
    parent_key: str
    attachment_key: str | None
    state: JobState = JobState.ZOTERO_SYNC
    diagnostic: str | None = None

    @property
    def zotero_uri(self) -> str:
        return f"zotero://select/library/items/{self.parent_key}"


def _item_data(item: object) -> Mapping[str, object]:
    if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
        raise ZoteroError("Zotero API returned malformed item data")
    return item["data"]


def _item_key(item: object) -> str:
    data = _item_data(item)
    key = item.get("key") if isinstance(item, dict) else None
    key = key or data.get("key")
    if not isinstance(key, str) or not key:
        raise ZoteroError("Zotero API returned an item without a key")
    return key


def _item_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ZoteroError("Zotero API returned malformed item list")
    return value


def _is_pdf_attachment(item: object) -> bool:
    data = _item_data(item)
    return data.get("itemType") == "attachment" and str(data.get("contentType", "")).lower() == "application/pdf"


def _metadata(item: object) -> PaperMetadata:
    data = _item_data(item)
    creators = data.get("creators")
    authors: list[str] = []
    if isinstance(creators, list):
        for creator in creators:
            if isinstance(creator, dict):
                name = " ".join(str(creator.get(name_part, "")).strip() for name_part in ("firstName", "lastName")).strip()
                authors.append(name or str(creator.get("name", "")).strip())
    return PaperMetadata(
        title=_as_string(data.get("title")), authors=[author for author in authors if author],
        published_date=_as_string(data.get("date")), doi=_as_string(data.get("DOI")),
        arxiv_id=_as_string(data.get("archiveID")), source_url=_as_string(data.get("url")),
        zotero_library_id="0", zotero_item_key=_item_key(item),
    )


def _as_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _json_response(response: ZoteroHttpResponse, operation: str) -> object:
    if not 200 <= response.status_code < 300:
        raise ZoteroError(f"Zotero {operation} failed with HTTP {response.status_code}")
    try:
        return json.loads(response.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZoteroError(f"Zotero {operation} returned malformed JSON") from exc


def _created_key(value: object) -> str:
    try:
        key = value["successful"]["0"]["key"]
    except (KeyError, TypeError):
        raise ZoteroError("Zotero item creation did not return a key") from None
    if not isinstance(key, str) or not key:
        raise ZoteroError("Zotero item creation did not return a key")
    return key
