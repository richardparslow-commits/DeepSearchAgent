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

from .models import CitatorCheck

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_kind(kind: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", kind.strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("snapshot kind must contain safe characters")
    return cleaned


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
        digest = hashlib.sha256(
            f"{safe_kind}\0{serialized}".encode("utf-8")
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
        except OSError as exc:
            # Snapshotting must never make a provider result fail. The caller
            # still receives the response; the failure is logged for the run.
            logger.warning("Could not persist %s snapshot: %s", kind, exc)

    def lookup_citations(self, text: str) -> Any:
        request = {"citation_text": text}
        try:
            response = self.provider.lookup_citations(text)
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture("courtlistener.lookup", request, {"error": str(exc)})
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
            self._capture("courtlistener.citing_edges", request, {"error": str(exc)})
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
                "courtlistener.same_docket", request, {"error": str(exc)}
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
                "courtlistener.opinion_metadata", request, {"error": str(exc)}
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

    def fetch_opinion_text(self, opinion_id: int) -> Any:
        request = {"opinion_id": opinion_id}
        try:
            response = self.provider.fetch_opinion_text(opinion_id)
        except Exception as exc:  # noqa: BLE001 - preserve provider error
            self._capture(
                "courtlistener.opinion_text", request, {"error": str(exc)}
            )
            raise
        self._capture("courtlistener.opinion_text", request, response)
        return response
