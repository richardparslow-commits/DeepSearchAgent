"""Durable evidence and source-response snapshots.

A snapshot is content-addressed: the manifest is append-only, while each
payload is written atomically to ``snapshots/<kind>/<id>.json``. Response
snapshots capture public provider payloads and request identifiers, never
authentication headers or API keys. Evidence snapshots capture the normalized
citator/history record that a reviewer can compare with those responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .models import CitatorCheck, LegalAnalysis
from .redaction import redact_secrets

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_kind(kind: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", kind.strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("snapshot kind must contain safe characters")
    return cleaned


def _normalize_source_url(url: str | None) -> str:
    """Canonicalize a snapshot's source URL for deduplication.

    The URL is part of the snapshot identity: the same payload retrieved from
    an official opinion and from a secondary commentary page must not collapse
    into one manifest entry. Scheme and host are case-insensitive, default
    ports and empty fragments are not meaningful, and a trailing slash on a
    non-root path is normalized. Path, query, and fragment content is otherwise
    preserved so distinct documents remain distinct.
    """
    if not isinstance(url, str):
        return ""
    value = url.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme and not parts.netloc:
        return value
    host = parts.hostname or ""
    if not host:
        return value
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        port = None

    host_display = f"[{host}]" if ":" in host and not host.startswith("[") else host
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"
    elif parts.password is not None:
        userinfo = f":{parts.password}@"
    netloc = f"{userinfo}{host_display}"
    if port is not None:
        netloc += f":{port}"

    path = parts.path.rstrip("/")
    return urlunsplit(
        (parts.scheme.lower(), netloc, path, parts.query, parts.fragment)
    )


@dataclass(frozen=True)
class SnapshotRecord:
    """One manifest entry pointing at an immutable snapshot payload."""

    id: str
    kind: str
    captured_at: str
    path: str
    source_url: str
    sha256: str
    metadata: dict[str, Any]


class SnapshotStore:
    """Append-only manifest and content-addressed snapshot payload store."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / "manifest.jsonl"
        self._snapshots = self.root / "snapshots"

    def _payload_path(self, kind: str, snapshot_id: str) -> Path:
        directory = self._snapshots / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{snapshot_id}.json"

    def _write_atomic(self, path: Path, document: dict[str, Any]) -> None:
        temporary = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                json.dump(
                    document,
                    temporary,
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _manifest_has_id(self, snapshot_id: str) -> bool:
        """Whether the manifest already carries a row for *snapshot_id*."""
        if not self.manifest.exists():
            return False
        try:
            with self.manifest.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get("id") == snapshot_id:
                        return True
        except OSError:
            return False
        return False

    def _append_manifest(self, record: SnapshotRecord) -> None:
        entry = {
            "id": record.id,
            "kind": record.kind,
            "captured_at": record.captured_at,
            "path": record.path,
            "source_url": record.source_url,
            "sha256": record.sha256,
            "metadata": record.metadata,
        }
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    entry, ensure_ascii=False, sort_keys=True, default=str
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def record(
        self,
        kind: str,
        payload: Any,
        *,
        source_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SnapshotRecord:
        """Persist one payload and append its manifest entry.

        Repeating the same kind + payload returns the existing immutable
        snapshot without appending a duplicate manifest row.
        """
        safe_kind = _safe_kind(kind)
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        )
        normalized_source_url = _normalize_source_url(source_url)
        digest = hashlib.sha256(
            f"{safe_kind}\0{normalized_source_url}\0{serialized}".encode("utf-8")
        ).hexdigest()
        snapshot_id = digest[:32]
        path = self._payload_path(safe_kind, snapshot_id)
        record = SnapshotRecord(
            id=snapshot_id,
            kind=safe_kind,
            captured_at=_now(),
            path=str(path.relative_to(self.root)),
            source_url=source_url,
            sha256=digest,
            metadata=metadata or {},
        )
        if path.exists():
            # The payload is immutable, but the manifest may have lost this
            # row (truncation/manual edit); re-append it so the snapshot stays
            # discoverable instead of silently orphaned.
            if not self._manifest_has_id(snapshot_id):
                self._append_manifest(record)
            return record
        self._write_atomic(
            path,
            {
                "id": snapshot_id,
                "kind": safe_kind,
                "captured_at": record.captured_at,
                "source_url": source_url,
                "sha256": digest,
                "metadata": record.metadata,
                "payload": payload,
            },
        )
        self._append_manifest(record)
        return record

    def record_evidence(
        self, check: CitatorCheck, *, source: str = ""
    ) -> SnapshotRecord:
        """Persist the normalized evidence behind one citator check."""
        return self.record(
            "evidence.citator",
            {"check": check.model_dump(mode="json")},
            source_url=check.target_url,
            metadata={
                "target_case": check.target_case,
                "target_cluster_id": check.target_cluster_id,
                "status": check.status,
                "source": source,
            },
        )

    def record_response(
        self,
        kind: str,
        request: dict[str, Any],
        response: Any,
        *,
        source_url: str = "",
    ) -> SnapshotRecord:
        """Persist one public provider request/response pair."""
        return self.record(
            kind,
            {"request": request, "response": response},
            source_url=source_url,
        )

    def record_analysis(self, analysis: LegalAnalysis) -> SnapshotRecord:
        """Persist the complete analysis contract, including D1 metadata.

        The normal JSON renderer already serializes these fields; this snapshot
        keeps the same lossless payload beside the evidence that produced it so
        a later audit can compare a surfaced case's citation, transcript URL,
        structured abstract, and provision impacts without re-running retrieval.
        """
        payload = analysis_snapshot_payload(analysis)
        return self.record(
            "analysis.report",
            payload,
            source_url=(analysis.surfaced_cases[0].retrieved_url or "")
            if analysis.surfaced_cases
            else "",
            metadata={
                "run_id": analysis.run_id,
                "issue": analysis.issue,
                "surfaced_case_count": len(analysis.surfaced_cases),
                "provision_impact_count": len(analysis.provision_impacts),
            },
        )


def analysis_snapshot_payload(analysis: LegalAnalysis) -> dict[str, Any]:
    """Return the lossless JSON-safe analysis payload used by snapshots."""
    return analysis.model_dump(mode="json")


class SnapshottingCourtListenerProvider:
    """Provider proxy that persists successful and failed public responses."""

    def __init__(self, provider: Any, store: SnapshotStore):
        self.provider = provider
        self.store = store

    def _capture(
        self,
        kind: str,
        request: dict[str, Any],
        response: Any,
        *,
        source_url: str = "",
    ) -> None:
        try:
            self.store.record_response(
                kind, request, response, source_url=source_url
            )
        except (OSError, TypeError, ValueError) as exc:
            # Snapshotting must never make a provider result fail. The caller
            # still receives the response; the failure is logged for the run.
            logger.warning(
                "Could not persist %s snapshot: %s", kind, redact_secrets(str(exc))
            )

    def lookup_citations(self, text: str) -> Any:
        request = {"citation_text": text}
        try:
            response = self.provider.lookup_citations(text)
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture("courtlistener.lookup", request, {"error": redact_secrets(str(exc))})
            raise
        self._capture("courtlistener.lookup", request, response)
        return response

    def citing_edges(
        self,
        opinion_id: int,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> Any:
        request = {
            "opinion_id": opinion_id,
            "page_size": page_size,
            "cursor": cursor,
        }
        try:
            response = self.provider.citing_edges(
                opinion_id, page_size=page_size, cursor=cursor
            )
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture("courtlistener.citing_edges", request, {"error": redact_secrets(str(exc))})
            raise
        self._capture("courtlistener.citing_edges", request, response)
        return response

    def same_docket_decisions(
        self, opinion_id: int, max_results: int = 5
    ) -> Any:
        request = {"opinion_id": opinion_id, "max_results": max_results}
        try:
            response = self.provider.same_docket_decisions(
                opinion_id, max_results=max_results
            )
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture(
                "courtlistener.same_docket", request, {"error": redact_secrets(str(exc))}
            )
            raise
        self._capture("courtlistener.same_docket", request, response)
        return response

    def opinion_metadata(self, opinion_id: int) -> Any:
        request = {"opinion_id": opinion_id}
        try:
            response = self.provider.opinion_metadata(opinion_id)
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture(
                "courtlistener.opinion_metadata", request, {"error": redact_secrets(str(exc))}
            )
            raise
        source_url = str(response.get("url") or "") if isinstance(response, dict) else ""
        self._capture(
            "courtlistener.opinion_metadata",
            request,
            response,
            source_url=source_url,
        )
        return response

    def opinion_metadata_batch(self, opinion_ids: list[int]) -> Any:
        request = {"opinion_ids": list(opinion_ids)}
        batch = getattr(self.provider, "opinion_metadata_batch", None)
        try:
            if batch is None:
                response = {
                    opinion_id: self.opinion_metadata(opinion_id)
                    for opinion_id in opinion_ids
                }
            else:
                response = batch(opinion_ids)
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture(
                "courtlistener.opinion_metadata_batch",
                request,
                {"error": redact_secrets(str(exc))},
            )
            raise
        self._capture(
            "courtlistener.opinion_metadata_batch", request, response
        )
        return response

    def fetch_opinion_text(self, opinion_id: int) -> Any:
        request = {"opinion_id": opinion_id}
        try:
            response = self.provider.fetch_opinion_text(opinion_id)
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture(
                "courtlistener.opinion_text", request, {"error": redact_secrets(str(exc))}
            )
            raise
        self._capture("courtlistener.opinion_text", request, response)
        return response
