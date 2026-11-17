"""Optional Regulations.gov rulemaking context for cited 38 C.F.R. parts.

The client is read-only. It maps Federal Register documents and cited 38 C.F.R.
parts to Regulations.gov dockets, retrieves docket metadata and Supporting &
Related Material documents, obtains comment counts from the comments endpoint's
``meta.totalElements``, and samples a few public comments. It never posts a
comment, never reads claimant information, and never treats public comments as
legal authority.

The official Regulations.gov GET limit is 1,000 requests/hour (Data.gov).
This client adds a lower per-run request cap and caches public responses. See
``REGULATIONS_RATE_LIMIT_URL``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

from .config import get_settings
from .courtlistener_cache import CourtListenerCache
from .models import (
    CaseRecord,
    FederalRegisterDocument,
    LegalAnalysis,
    ProvisionImpact,
    RegulationsDocket,
    RegulationsDocument,
    RegulationsMapping,
    RegulationsPublicComment,
    RegulationsSupportingDocument,
)
from .redaction import redact_secrets, sanitize_exception
from .search import SearchError, http_proxy_kwargs

def provision_level_impacts(cases: list[CaseRecord]) -> list[ProvisionImpact]:
    """Return the structured 38 U.S.C./C.F.R./M21-1 impact layer.

    This module remains the read-only Regulations.gov rulemaking client.  The
    provision analyzer lives in :mod:`va_legal_agent.va_domain` so a docket
    lookup and a legal-impact mapping cannot be confused: a rulemaking docket
    is context, while a provision impact names the adjudication stage and the
    opinion sentence that supports it.
    """
    from .va_domain import impacts_from_cases

    return impacts_from_cases(cases)


FEDERAL_REGISTER_DOCUMENTS_URL = (
    "https://www.federalregister.gov/api/v1/documents.json"
)
REGULATIONS_API_BASE = "https://api.regulations.gov/v4"
REGULATIONS_RATE_LIMIT_PER_HOUR = 1_000
REGULATIONS_RATE_LIMIT_URL = "https://www.regulations.gov/faq?anchor=regulatory"
REGULATIONS_DOCUMENT_URL = "https://www.regulations.gov/document/{document_id}"
REGULATIONS_DOCKET_URL = "https://www.regulations.gov/docket/{docket_id}"

_MAX_FR_DOCUMENTS = 3
_MAX_REGULATIONS_DOCUMENTS = 5
_SUPPORTING_DOCUMENT_LIMIT = 10
_COMMENT_EXCERPT_LIMIT = 500


@dataclass(frozen=True)
class RegulationsBundle:
    """Rulemaking records produced for one analysis."""

    mappings: list[RegulationsMapping] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().user_agent}


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _excerpt(value: object) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= _COMMENT_EXCERPT_LIMIT:
        return text
    return text[: _COMMENT_EXCERPT_LIMIT - 3].rstrip() + "..."


def _url(value: object) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return f"https://{text.lstrip('/')}"


def _docket_ids(row: dict[str, object]) -> list[str]:
    raw = row.get("docket_ids")
    values: list[str] = []
    if isinstance(raw, list):
        values.extend(_text(value) for value in raw)
    info = _as_dict(row.get("regulations_dot_gov_info"))
    values.extend(
        _text(info.get(key)) for key in ("docket_id", "docketId")
    )
    values.extend(
        _text(row.get(key)) for key in ("docket_id", "docketId")
    )
    return list(dict.fromkeys(value for value in values if value))


class RegulationsClient:
    """Cached, read-only Regulations.gov client.

    ``REGULATIONS_API_KEY`` is the opt-in switch. Only GET requests are made;
    there is no code path for posting a comment or uploading a file.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cache_dir: str | None = None,
        cache_ttl_seconds: int | None = None,
        max_requests: int | None = None,
        comment_sample_limit: int | None = None,
        lookback_days: int | None = None,
    ):
        settings = get_settings()
        self.api_key = (
            api_key if api_key is not None else settings.regulations_api_key
        ) or ""
        self.max_requests = (
            max_requests
            if max_requests is not None
            else settings.regulations_max_requests_per_run
        )
        self.comment_sample_limit = (
            comment_sample_limit
            if comment_sample_limit is not None
            else settings.regulations_comment_sample_limit
        )
        self.lookback_days = (
            lookback_days
            if lookback_days is not None
            else settings.federal_register_lookback_days
        )
        self.requests_made = 0
        self.errors: list[str] = []
        self._cache: CourtListenerCache | None = None

        directory = (
            cache_dir
            if cache_dir is not None
            else settings.regulations_cache_dir
        )
        ttl = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else settings.regulations_cache_ttl_seconds
        )
        directory = (directory or "").strip()
        if self.api_key and directory and ttl > 0:
            try:
                self._cache = CourtListenerCache(directory, ttl)
            except OSError:
                self._cache = None

    def _note_error(self, message: str) -> None:
        if message and message not in self.errors:
            self.errors.append(message)

    def _cache_get(self, key: str, params: dict[str, object]) -> Any:
        if self._cache is None:
            return None
        try:
            return self._cache.get("regulations", key, params)
        except OSError:
            return None

    def _cache_set(
        self, key: str, params: dict[str, object], payload: object
    ) -> None:
        if self._cache is None:
            return
        try:
            self._cache.set("regulations", key, payload, params=params)
        except OSError:
            pass

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, object],
        cache_key: str,
        source_name: str,
        api_key: str = "",
    ) -> dict[str, object]:
        cached = self._cache_get(cache_key, params)
        if cached is not None:
            return cached if isinstance(cached, dict) else {}
        if self.requests_made >= self.max_requests:
            raise SearchError(
                "Regulations.gov request cap reached for this analysis "
                f"({self.max_requests}); cached results were retained."
            )
        self.requests_made += 1
        headers = _headers()
        if api_key:
            headers["X-Api-Key"] = api_key
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=(10, get_settings().request_timeout_seconds),
                **http_proxy_kwargs(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            sanitize_exception(exc, (api_key,))
            detail = redact_secrets(str(exc), (api_key,))
            raise SearchError(f"{source_name} request failed: {detail}") from exc
        if not isinstance(payload, dict):
            raise SearchError(
                f"{source_name} returned a non-object JSON response."
            )
        self._cache_set(cache_key, params, payload)
        return payload

    def _federal_register_documents(
        self, part: str
    ) -> list[FederalRegisterDocument]:
        today = datetime.now(timezone.utc).date()
        since = (today - timedelta(days=self.lookback_days)).isoformat()
        payload = self._request_json(
            FEDERAL_REGISTER_DOCUMENTS_URL,
            params={
                "conditions[cfr][title]": "38",
                "conditions[cfr][part]": part,
                "conditions[type][]": ["RULE", "PRORULE"],
                "conditions[publication_date][gte]": since,
                "order": "newest",
                "per_page": _MAX_FR_DOCUMENTS,
                "fields[]": [
                    "document_number",
                    "title",
                    "publication_date",
                    "type",
                    "html_url",
                    "abstract",
                    "action",
                    "docket_ids",
                    "docket_id",
                    "regulations_dot_gov_info",
                ],
            },
            cache_key=f"federal-register:part:{part}",
            source_name="Federal Register",
        )
        documents: list[FederalRegisterDocument] = []
        for row in _as_list(payload.get("results")):
            if not isinstance(row, dict):
                continue
            number = _text(row.get("document_number"))
            if not number:
                continue
            documents.append(
                FederalRegisterDocument(
                    document_number=number,
                    title=_text(row.get("title")),
                    publication_date=_text(row.get("publication_date")),
                    type=_text(row.get("type")),
                    url=_url(row.get("html_url")),
                    abstract=_text(row.get("abstract")),
                    action=_text(row.get("action")),
                    docket_ids=_docket_ids(row),
                )
            )
            if len(documents) >= _MAX_FR_DOCUMENTS:
                break
        return documents

    def _regulations_documents(
        self, fr_document_number: str
    ) -> list[RegulationsDocument]:
        payload = self._request_json(
            f"{REGULATIONS_API_BASE}/documents",
            params={
                "filter[frDocNum]": fr_document_number,
                "page[size]": 5,
            },
            cache_key=f"documents:fr:{fr_document_number}",
            source_name="Regulations.gov",
            api_key=self.api_key,
        )
        documents: list[RegulationsDocument] = []
        for item in _as_list(payload.get("data")):
            if not isinstance(item, dict):
                continue
            attributes = _as_dict(item.get("attributes"))
            document_id = _text(item.get("id") or attributes.get("documentId"))
            if not document_id:
                continue
            docket_id = _text(
                attributes.get("docketId") or attributes.get("docket_id")
            )
            documents.append(
                RegulationsDocument(
                    document_id=document_id,
                    docket_id=docket_id,
                    fr_document_number=_text(
                        attributes.get("frDocNum") or fr_document_number
                    ),
                    title=_text(attributes.get("title")),
                    document_type=_text(attributes.get("documentType")),
                    posted_date=_text(attributes.get("postedDate")),
                    comment_start_date=_text(
                        attributes.get("commentStartDate")
                    ),
                    comment_end_date=_text(attributes.get("commentEndDate")),
                    open_for_comment=_as_bool(attributes.get("openForComment")),
                    url=REGULATIONS_DOCUMENT_URL.format(
                        document_id=quote(document_id, safe="")
                    ),
                    object_id=_text(attributes.get("objectId")),
                )
            )
            if len(documents) >= _MAX_REGULATIONS_DOCUMENTS:
                break
        return documents

    def _supporting_documents(
        self, docket_id: str
    ) -> list[RegulationsSupportingDocument]:
        payload = self._request_json(
            f"{REGULATIONS_API_BASE}/documents",
            params={
                "filter[docketId]": docket_id,
                "filter[documentType]": "Supporting & Related Material",
                "page[size]": _SUPPORTING_DOCUMENT_LIMIT,
            },
            cache_key=f"documents:docket:{docket_id}:supporting",
            source_name="Regulations.gov",
            api_key=self.api_key,
        )
        documents: list[RegulationsSupportingDocument] = []
        for item in _as_list(payload.get("data")):
            if not isinstance(item, dict):
                continue
            attributes = _as_dict(item.get("attributes"))
            document_id = _text(item.get("id") or attributes.get("documentId"))
            if not document_id:
                continue
            documents.append(
                RegulationsSupportingDocument(
                    document_id=document_id,
                    title=_text(attributes.get("title")),
                    document_type=_text(attributes.get("documentType")),
                    posted_date=_text(attributes.get("postedDate")),
                    url=REGULATIONS_DOCUMENT_URL.format(
                        document_id=quote(document_id, safe="")
                    ),
                )
            )
            if len(documents) >= _SUPPORTING_DOCUMENT_LIMIT:
                break
        return documents

    def _docket(self, docket_id: str) -> RegulationsDocket:
        payload = self._request_json(
            f"{REGULATIONS_API_BASE}/dockets/{quote(docket_id, safe='')}",
            params={},
            cache_key=f"docket:{docket_id}",
            source_name="Regulations.gov",
            api_key=self.api_key,
        )
        attributes = _as_dict(_as_dict(payload.get("data")).get("attributes"))
        docket = RegulationsDocket(
            docket_id=docket_id,
            title=_text(attributes.get("title")),
            agency_id=_text(attributes.get("agencyId")),
            docket_type=_text(attributes.get("docketType")),
            rin=_text(attributes.get("rin")),
            url=REGULATIONS_DOCKET_URL.format(
                docket_id=quote(docket_id, safe="")
            ),
        )
        try:
            docket.supporting_documents = self._supporting_documents(docket_id)
        except SearchError as exc:
            docket.status = "partial"
            docket.detail = f"Supporting documents unavailable: {exc}"
            self._note_error(docket.detail)
        return docket

    def _comments(self, document: RegulationsDocument) -> None:
        if not document.object_id:
            document.status = "comments_unavailable"
            document.detail = (
                "No Regulations.gov objectId was returned, so comments "
                "could not be queried."
            )
            return
        page_size = max(5, self.comment_sample_limit)
        try:
            payload = self._request_json(
                f"{REGULATIONS_API_BASE}/comments",
                params={
                    "filter[commentOnId]": document.object_id,
                    "page[size]": page_size,
                    "sort": "-postedDate",
                },
                cache_key=f"comments:{document.object_id}",
                source_name="Regulations.gov",
                api_key=self.api_key,
            )
        except SearchError as exc:
            document.status = "comments_unavailable"
            document.detail = f"Public comments unavailable: {exc}"
            self._note_error(document.detail)
            return

        meta = _as_dict(payload.get("meta"))
        document.comment_count = _as_int(meta.get("totalElements"))
        comments: list[RegulationsPublicComment] = []
        for item in _as_list(payload.get("data")):
            if not isinstance(item, dict):
                continue
            attributes = _as_dict(item.get("attributes"))
            comment_id = _text(
                item.get("id") or attributes.get("trackingNbr")
            )
            comments.append(
                RegulationsPublicComment(
                    comment_id=comment_id,
                    title=_text(attributes.get("title")),
                    posted_date=_text(attributes.get("postedDate")),
                    excerpt=_excerpt(
                        attributes.get("comment") or attributes.get("title")
                    ),
                )
            )
            if len(comments) >= self.comment_sample_limit:
                break
        document.comments = comments
        document.comments_sampled = len(comments)
        if "totalElements" not in meta:
            document.detail = (
                "Comment count was not supplied by the API; the sampled "
                "count is not a total."
            )

    def map_part(self, part: str) -> RegulationsMapping:
        """Map one 38 C.F.R. part to its rulemaking dockets."""
        mapping = RegulationsMapping(cfr_part=part)
        if not self.api_key:
            mapping.status = "not_assessed"
            mapping.detail = (
                "REGULATIONS_API_KEY is not set, so rulemaking context was "
                "not retrieved."
            )
            return mapping
        try:
            fr_documents = self._federal_register_documents(part)
        except SearchError as exc:
            mapping.status = "unavailable"
            mapping.detail = str(exc)
            return mapping
        mapping.fr_documents = fr_documents
        if not fr_documents:
            mapping.status = "no_docket"
            mapping.detail = (
                f"No Federal Register rule or proposed rule affecting "
                f"38 C.F.R. Part {part} was found in the lookback window."
            )
            return mapping

        dockets: dict[str, RegulationsDocket] = {}
        mapped_count = 0
        for fr_document in fr_documents:
            try:
                regulations_documents = self._regulations_documents(
                    fr_document.document_number
                )
            except SearchError as exc:
                self._note_error(str(exc))
                continue
            for document in regulations_documents:
                if not document.docket_id:
                    document.status = "no_docket"
                    document.detail = (
                        "No Regulations.gov docket ID was returned for this "
                        "Federal Register document."
                    )
                    continue
                mapped_count += 1
                docket = dockets.get(document.docket_id)
                if docket is None:
                    try:
                        docket = self._docket(document.docket_id)
                    except SearchError as exc:
                        docket = RegulationsDocket(
                            docket_id=document.docket_id,
                            title=document.title,
                            status="partial",
                            detail=f"Docket metadata unavailable: {exc}",
                        )
                        self._note_error(docket.detail)
                    dockets[document.docket_id] = docket
                self._comments(document)
                docket.documents.append(document)

        mapping.dockets = list(dockets.values())
        if not mapping.dockets:
            mapping.status = "no_docket"
            mapping.detail = (
                f"Found {len(fr_documents)} Federal Register document(s), "
                "but none mapped to a Regulations.gov docket."
            )
        else:
            mapping.status = "mapped"
            mapping.detail = (
                f"Found {len(fr_documents)} Federal Register document(s); "
                f"mapped {mapped_count} to {len(mapping.dockets)} docket(s)."
            )
        return mapping


def regulations_flags(
    mappings: list[RegulationsMapping], errors: list[str] | None = None
) -> list[str]:
    """Surface missing coverage without treating it as an authoritative result."""
    flags: list[str] = []
    seen: set[str] = set()
    for mapping in mappings:
        if mapping.status not in {"unavailable", "not_assessed"}:
            continue
        flag = (
            f"Regulations.gov context {mapping.status} for "
            f"38 C.F.R. Part {mapping.cfr_part}: {mapping.detail}"
        )
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    for error in errors or []:
        flag = f"Regulations.gov source error: {error}"
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    return flags


def verify_analysis_regulations(
    analysis: LegalAnalysis, cases: list[CaseRecord]
) -> RegulationsBundle:
    """Map cited 38 C.F.R. parts to Regulations.gov rulemaking records.

    Gated by ``AUTHORITY_VERIFICATION`` and ``REGULATIONS_API_KEY``. Without a
    key the result is explicit ``not_assessed`` coverage, not an empty clean
    finding. Public comments remain ``public_input`` and never become authority.
    """
    settings = get_settings()
    if not settings.authority_verification:
        return RegulationsBundle()

    from .authorities import _analysis_text, extract_cfr_sections

    text = _analysis_text(analysis, cases)
    parts = list(
        dict.fromkeys(
            section.split(".", 1)[0]
            for section in extract_cfr_sections(text)
        )
    )
    if not parts:
        return RegulationsBundle()

    client = RegulationsClient()
    mappings = [client.map_part(part) for part in parts]
    return RegulationsBundle(
        mappings=mappings,
        flags=regulations_flags(mappings, client.errors),
    )
