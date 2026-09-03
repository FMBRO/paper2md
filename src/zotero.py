"""Zotero local API access, isolated behind an injectable HTTP client."""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Protocol
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


class ZoteroError(RuntimeError):
    """The local Zotero API could not fulfill an unambiguous request."""


class ZoteroInputError(ZoteroError):
    """The requested Zotero item or attachment needs user correction."""


class UrlLibZoteroHttpClient:
    """Small production transport; ``httpx.Client`` is also accepted by the client."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> ZoteroHttpResponse:
        request = urllib.request.Request(url, data=content, headers=dict(headers or {}), method=method)
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=5) as response:
                return ZoteroHttpResponse(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            return ZoteroHttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())


class ZoteroClient:
    """Client for a personal Zotero local API library (API version 3)."""

    def __init__(
        self,
        settings: ZoteroSettings | None = None,
        *,
        http_client: ZoteroHttpClient | None = None,
        operation_store: Any | None = None,
    ) -> None:
        settings = settings or ZoteroSettings()
        self.base_url = settings.base_url.rstrip("/") + "/"
        self.user_id = settings.user_id
        self.http_client = http_client or UrlLibZoteroHttpClient()
        self.operation_store = operation_store

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
                raise ZoteroInputError("Selected attachment is not a PDF child of the requested Zotero item")
            return self._resolve_attachment(attachment, parent=item)
        children = self._get_json(f"users/{self.user_id}/items/{parent_key}/children")
        attachments = [child for child in _item_list(children) if _is_pdf_attachment(child)]
        if len(attachments) != 1:
            return self._needs_input(parent_key, _metadata(item, str(self.user_id)), None, "No PDF attachment found" if not attachments else "Multiple PDF attachments found; select one")
        attachment_key = _item_key(attachments[0])
        path = self._attachment_path(attachment_key)
        if path is None:
            return self._needs_input(parent_key, _metadata(item, str(self.user_id)), attachment_key, "Zotero PDF file is missing or inaccessible")
        return ZoteroResolution(_metadata(item, str(self.user_id)), path, parent_key, attachment_key)

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
        if not source.is_file():
            raise FileNotFoundError(f"Source PDF does not exist: {source}")
        data = source.read_bytes()
        if self.operation_store is not None:
            return self._durable_upsert_non_zotero(metadata, source, data)
        existing = self._find_existing_parent(metadata)
        if existing is not None:
            return ZoteroResolution(
                _metadata(existing, str(self.user_id)), source,
                _item_key(existing), None,
            )
        raise ZoteroError("A durable operation store is required for Zotero writes")

    def _durable_upsert_non_zotero(
        self, metadata: PaperMetadata, source: Path, data: bytes,
    ) -> "ZoteroResolution":
        source_sha256 = hashlib.sha256(data).hexdigest()
        canonical_identity = metadata.canonical_identity(source_sha256)
        if canonical_identity is None:
            raise ZoteroError("Cannot derive a durable Zotero operation identity")
        operation = self.operation_store.get_zotero_operation(
            canonical_identity, source_sha256,
        )
        if operation is None:
            existing = self._find_existing_parent(metadata)
            operation_id = hashlib.sha256(
                f"{self.user_id}\0{canonical_identity}\0{source_sha256}".encode()
            ).hexdigest()
            operation = self.operation_store.create_zotero_operation(
                operation_id=operation_id,
                canonical_identity=canonical_identity,
                source_sha256=source_sha256,
                parent_key=(
                    _item_key(existing)
                    if existing is not None
                    else _stable_zotero_key(operation_id, "parent")
                ),
                attachment_key=_stable_zotero_key(operation_id, "attachment"),
                parent_write_token=_stable_token(operation_id, "parent"),
                attachment_write_token=_stable_token(operation_id, "attachment"),
                upload_write_token=_stable_token(operation_id, "upload"),
                registration_write_token=_stable_token(operation_id, "registration"),
            )
        parent_key = str(operation["parent_key"])
        attachment_key = str(operation["attachment_key"])
        if operation["step"] == "complete":
            return self._created_resolution(metadata, source, parent_key, attachment_key)

        def checkpoint(step: str, state: dict[str, Any] | None = None) -> None:
            nonlocal operation
            operation = self.operation_store.checkpoint_zotero_operation(
                operation["id"], step, state,
            )

        parent_item = self._optional_item(parent_key)
        if parent_item is not None:
            if _item_data(parent_item).get("itemType") == "attachment":
                raise ZoteroError("Planned Zotero parent key is occupied by an attachment")
            checkpoint("parent_reconciled", {"parent_created": True})
        attachment_item = self._optional_item(attachment_key)
        if attachment_item is not None:
            attachment_data = _item_data(attachment_item)
            if (
                not _is_pdf_attachment(attachment_item)
                or attachment_data.get("parentItem") != parent_key
            ):
                raise ZoteroError("Planned Zotero attachment key is occupied by another item")
            checkpoint("attachment_reconciled", {"attachment_created": True})

        server_id: str | None = None
        api_key: str | None = None
        reusable = False
        used = False

        def authorize(action: str) -> str:
            nonlocal server_id, reusable
            checkpoint(f"{action}_authorization_planned")
            if server_id is None:
                probe = self._request("GET", "")
                server_id = _header_value(probe.headers, "Zotero-Server-ID")
                if not server_id:
                    raise ZoteroError(
                        "Zotero local API did not provide Zotero-Server-ID required for writes"
                    )
            value = _json_response(
                self._request(
                    "POST", "local/authorize", content=b'{"appName":"paper2md"}',
                    headers={
                        "Content-Type": "application/json",
                        "Zotero-Server-ID": server_id,
                    },
                ),
                "write authorization",
            )
            token = value.get("key") if isinstance(value, dict) else None
            if not isinstance(token, str):
                raise ZoteroError("Zotero write authorization was denied")
            reusable = bool(value.get("remember"))
            return token

        def write(
            path: str,
            content: bytes,
            headers: Mapping[str, str],
            *,
            write_token: str,
            action: str,
        ) -> ZoteroHttpResponse:
            nonlocal api_key, used
            if api_key is None or (used and not reusable):
                api_key = authorize(action)
            response = self._request(
                "POST", path, content=content,
                headers={
                    "Zotero-Server-ID": str(server_id),
                    "Zotero-API-Key": api_key,
                    "Zotero-Write-Token": write_token,
                    **headers,
                },
            )
            used = True
            if not 200 <= response.status_code < 300:
                raise ZoteroError(
                    f"Zotero API returned HTTP {response.status_code} for {path}"
                )
            return response

        if parent_item is None:
            parent: dict[str, object] = {
                "key": parent_key,
                "itemType": "journalArticle",
                "title": metadata.title or "Untitled",
            }
            if metadata.doi:
                parent["DOI"] = metadata.doi
            if metadata.arxiv_id:
                parent["archiveID"] = metadata.arxiv_id
            if metadata.authors:
                parent["creators"] = [
                    {"creatorType": "author", "name": author}
                    for author in metadata.authors
                ]
            if metadata.published_date:
                parent["date"] = metadata.published_date
            checkpoint("parent_creation_planned")
            created_key = _created_key(_json_response(
                write(
                    f"users/{self.user_id}/items", json.dumps([parent]).encode(),
                    {"Content-Type": "application/json"},
                    write_token=str(operation["parent_write_token"]),
                    action="parent_creation",
                ),
                "parent creation",
            ))
            if created_key != parent_key:
                raise ZoteroError("Zotero did not honor the planned parent key")
            checkpoint("parent_created", {"parent_created": True})

        if attachment_item is None:
            attachment = {
                "key": attachment_key,
                "itemType": "attachment",
                "parentItem": parent_key,
                "linkMode": "imported_file",
                "contentType": "application/pdf",
                "filename": source.name,
                "title": source.name,
            }
            checkpoint("attachment_creation_planned")
            created_key = _created_key(_json_response(
                write(
                    f"users/{self.user_id}/items", json.dumps([attachment]).encode(),
                    {"Content-Type": "application/json"},
                    write_token=str(operation["attachment_write_token"]),
                    action="attachment_creation",
                ),
                "attachment creation",
            ))
            if created_key != attachment_key:
                raise ZoteroError("Zotero did not honor the planned attachment key")
            checkpoint("attachment_created", {"attachment_created": True})

        file_path = f"users/{self.user_id}/items/{attachment_key}/file"
        form = urlencode({
            "md5": hashlib.md5(data).hexdigest(),
            "filename": source.name,
            "filesize": len(data),
            "mtime": int(source.stat().st_mtime * 1000),
        }).encode()
        checkpoint("upload_authorization_planned")
        upload = _json_response(
            write(
                file_path, form,
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "If-None-Match": "*",
                },
                write_token=str(operation["upload_write_token"]),
                action="upload",
            ),
            "file upload authorization",
        )
        if not isinstance(upload, dict):
            raise ZoteroError(
                "Zotero file upload authorization returned malformed JSON"
            )
        if upload.get("exists") == 1:
            checkpoint("complete", {"registered": True})
            return self._created_resolution(
                metadata, source, parent_key, attachment_key,
            )
        url, upload_key = upload.get("url"), upload.get("uploadKey")
        if not isinstance(url, str) or not isinstance(upload_key, str):
            raise ZoteroError(
                "Zotero file upload authorization returned malformed JSON"
            )
        upload_state = {
            "upload_url": url,
            "upload_key": upload_key,
            "upload_content_type": str(
                upload.get("contentType") or "application/pdf"
            ),
            "upload_prefix": str(upload.get("prefix", "")),
            "upload_suffix": str(upload.get("suffix", "")),
        }
        checkpoint("upload_authorized", upload_state)
        checkpoint("upload_planned")
        try:
            posted = self.http_client.request(
                "POST", url,
                headers={
                    "Content-Type": upload_state["upload_content_type"],
                    "Zotero-API-Version": "3",
                },
                content=(
                    upload_state["upload_prefix"].encode()
                    + data
                    + upload_state["upload_suffix"].encode()
                ),
            )
        except Exception as exc:
            raise ZoteroError(f"Zotero local API is unavailable: {exc}") from exc
        if posted.status_code != 201:
            raise ZoteroError(
                f"Zotero file upload failed with HTTP {posted.status_code}"
            )
        checkpoint("uploaded", {"uploaded": True})
        checkpoint("registration_planned")
        registered = write(
            file_path, urlencode({"upload": upload_key}).encode(),
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "If-None-Match": "*",
            },
            write_token=str(operation["registration_write_token"]),
            action="registration",
        )
        if registered.status_code != 204:
            raise ZoteroError(
                "Zotero file upload registration failed with "
                f"HTTP {registered.status_code}"
            )
        checkpoint("registered", {"registered": True})
        checkpoint("complete")
        return self._created_resolution(metadata, source, parent_key, attachment_key)

    def _find_existing_parent(self, metadata: PaperMetadata) -> object | None:
        identity = metadata.doi or metadata.arxiv_id
        if not identity:
            return None
        query = urlencode({
            "q": identity, "qmode": "everything", "itemType": "-attachment",
        })
        for item in _item_list(
            self._get_json(f"users/{self.user_id}/items?{query}")
        ):
            item_data = _item_data(item)
            candidate = PaperMetadata(
                doi=_as_string(item_data.get("DOI")),
                arxiv_id=_as_string(item_data.get("archiveID")),
            )
            if (
                (metadata.doi is not None and candidate.doi == metadata.doi)
                or (
                    metadata.arxiv_id is not None
                    and candidate.arxiv_id == metadata.arxiv_id
                )
            ):
                return item
        return None

    def _optional_item(self, key: str) -> object | None:
        response = self._request("GET", f"users/{self.user_id}/items/{key}")
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise ZoteroError(
                f"Zotero API returned HTTP {response.status_code} for item {key}"
            )
        try:
            return json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ZoteroError(
                f"Zotero API returned malformed JSON for item {key}"
            ) from exc

    def _created_resolution(
        self,
        metadata: PaperMetadata,
        source: Path,
        parent_key: str,
        attachment_key: str,
    ) -> "ZoteroResolution":
        created = PaperMetadata(
            title=metadata.title,
            authors=list(metadata.authors),
            published_date=metadata.published_date,
            published_date_raw=metadata.published_date_raw,
            doi=metadata.doi,
            arxiv_id=metadata.arxiv_id,
            source_url=metadata.source_url,
            zotero_library_id=str(self.user_id),
            zotero_item_key=parent_key,
        )
        return ZoteroResolution(created, source, parent_key, attachment_key)

    def _resolve_attachment(self, attachment: object, *, parent: object | None = None) -> "ZoteroResolution":
        if not _is_pdf_attachment(attachment):
            raise ZoteroInputError("Requested Zotero attachment is not a PDF")
        attachment_key = _item_key(attachment)
        parent_key = _item_data(attachment).get("parentItem")
        if not isinstance(parent_key, str) or not parent_key:
            parent_key = attachment_key
        if parent is None and parent_key != attachment_key:
            parent = self._get_json(f"users/{self.user_id}/items/{parent_key}")
        metadata = _metadata(parent or attachment, str(self.user_id))
        path = self._attachment_path(attachment_key)
        if path is None:
            return self._needs_input(parent_key, metadata, attachment_key, "Zotero PDF file is missing or inaccessible")
        return ZoteroResolution(metadata, path, parent_key, attachment_key)

    def _get_json(self, path: str) -> object:
        response = self._request("GET", path)
        if not 200 <= response.status_code < 300:
            if response.status_code == 404:
                raise ZoteroInputError(f"Zotero item was not found for {path}")
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
            if response.status_code == 404:
                raise ZoteroInputError(f"Zotero attachment {attachment_key} is missing")
            raise ZoteroError(f"Zotero API returned HTTP {response.status_code} for attachment file {attachment_key}")
        location = _header_value(response.headers, "Location")
        if not location or urlparse(location).scheme != "file":
            raise ZoteroInputError(
                f"Zotero attachment {attachment_key} did not return an accessible local file"
            )
        path = Path(file_url_to_path(location))
        return path if path.is_file() else None

    @staticmethod
    def _needs_input(parent_key: str, metadata: PaperMetadata, attachment_key: str | None,
                     diagnostic: str) -> "ZoteroResolution":
        return ZoteroResolution(metadata, None, parent_key, attachment_key, JobState.NEEDS_INPUT, diagnostic)


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


def _metadata(item: object, library_id: str = "0") -> PaperMetadata:
    data = _item_data(item)
    meta = item.get("meta") if isinstance(item, dict) else None
    creators = data.get("creators")
    authors: list[str] = []
    if isinstance(creators, list):
        for creator in creators:
            if isinstance(creator, dict):
                name = " ".join(str(creator.get(name_part, "")).strip() for name_part in ("firstName", "lastName")).strip()
                authors.append(name or str(creator.get("name", "")).strip())
    raw_date = _as_string(data.get("date"))
    parsed_date = _as_string(meta.get("parsedDate")) if isinstance(meta, dict) else None
    return PaperMetadata(
        title=_as_string(data.get("title")), authors=[author for author in authors if author],
        published_date=normalize_zotero_date(parsed_date) or normalize_zotero_date(raw_date),
        published_date_raw=raw_date, doi=_as_string(data.get("DOI")),
        arxiv_id=_as_string(data.get("archiveID")), source_url=_as_string(data.get("url")),
        zotero_library_id=library_id, zotero_item_key=_item_key(item),
    )


def _as_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def normalize_zotero_date(value: str | None) -> str | None:
    """Return a Notion-safe ISO date while preserving Zotero precision."""
    if not value:
        return None
    raw = value.strip()
    season = re.fullmatch(r"(spring|summer|autumn|fall|winter)\s+(\d{4})", raw, re.IGNORECASE)
    if season:
        month = {"spring": 3, "summer": 6, "autumn": 9, "fall": 9, "winter": 12}[
            season.group(1).casefold()
        ]
        try:
            date(int(season.group(2)), month, 1)
        except ValueError:
            return None
        return f"{season.group(2)}-{month:02d}"
    if re.fullmatch(r"\d{4}", raw):
        try:
            date(int(raw), 1, 1)
        except ValueError:
            return None
        return raw
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            date.fromisoformat(raw + "-01")
        except ValueError:
            return None
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            date.fromisoformat(raw)
        except ValueError:
            return None
        return raw
    return None


def file_url_to_path(value: str, *, platform: str = os.name) -> PurePath:
    """Decode a local ``file:`` URL without losing drive or UNC semantics."""
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "file":
        raise ValueError("Expected a file URL")
    decoded_path = unquote(parsed.path)
    if platform == "nt":
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            return PureWindowsPath(f"//{parsed.netloc}{decoded_path}")
        if re.match(r"^/[A-Za-z]:/", decoded_path):
            decoded_path = decoded_path[1:]
        return PureWindowsPath(decoded_path)
    if parsed.netloc and parsed.netloc.casefold() != "localhost":
        return PurePosixPath(f"//{parsed.netloc}{decoded_path}")
    return PurePosixPath(decoded_path)


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


_ZOTERO_KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"


def _stable_zotero_key(operation_id: str, purpose: str) -> str:
    digest = hashlib.sha256(f"{operation_id}:{purpose}".encode()).digest()
    return "".join(
        _ZOTERO_KEY_ALPHABET[value % len(_ZOTERO_KEY_ALPHABET)]
        for value in digest[:8]
    )


def _stable_token(operation_id: str, purpose: str) -> str:
    return hashlib.sha256(f"{operation_id}:write:{purpose}".encode()).hexdigest()
