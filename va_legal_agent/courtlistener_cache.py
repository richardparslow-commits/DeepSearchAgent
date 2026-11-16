"""Persistent read-through cache for CourtListener entity and edge responses.

The cache stores only public response payloads and request identifiers. It
never accepts or writes request headers, so ``Authorization`` tokens cannot be
persisted. Entries live in one JSON file per key, are replaced atomically, and
expire after a configurable TTL. Explicit invalidation can remove one key,
every entry of a kind, or the whole cache.

Only CourtListener opinion, cluster, docket, and citation-edge GET responses
are cached by the provider. Search, usage, and citation-lookup responses are
deliberately outside this layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

CACHE_VERSION = 1

# Query keys that could carry credentials are never written. CourtListener
# authenticates with a header, but this guard keeps a caller mistake from
# persisting a token in the request metadata of an otherwise valid cache entry.
_SENSITIVE_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "token",
    "x_api_key",
    "x-api-key",
}


def _safe_kind(kind: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (kind or "").strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("cache kind must contain safe characters")
    return cleaned


def _safe_mapping(values: dict[str, object] | None) -> dict[str, object]:
    if not values:
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if str(key).casefold() not in _SENSITIVE_KEYS
    }


def _safe_url(url: str) -> str:
    """Return *url* without credential-like query parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in _SENSITIVE_KEYS
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )


def _canonical_params(params: dict[str, object] | None) -> str:
    return json.dumps(
        _safe_mapping(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(kind: str, identifier: str, params: dict[str, object] | None) -> str:
    material = f"{kind}\0{identifier}\0{_canonical_params(params)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _remove(path: Path) -> bool:
    """Remove one cache file, treating filesystem failures as best-effort."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


class CourtListenerCache:
    """Disk-backed, TTL-bounded cache for public CourtListener responses.

    ``get`` returns ``None`` for a miss, an expired entry, or a corrupt entry.
    ``set`` is a no-op when ``ttl_seconds <= 0``. Reads and writes are
    best-effort: the provider treats cache failures as misses and still returns
    the live response.
    """

    def __init__(
        self,
        root: str | Path,
        ttl_seconds: int = 86_400,
        *,
        now: Callable[[], float] = time.time,
    ):
        self.root = Path(root)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._now = now
        self._entries = self.root / "entries"
        if self.enabled:
            self._entries.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def _path(
        self,
        kind: str,
        identifier: str,
        params: dict[str, object] | None = None,
    ) -> Path:
        return self._entries / f"{_digest(_safe_kind(kind), identifier, params)}.json"

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        if document.get("version") != CACHE_VERSION:
            return None
        return document

    def get(
        self,
        kind: str,
        identifier: str,
        params: dict[str, object] | None = None,
    ) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(kind, identifier, params)
        document = self._read(path)
        if document is None:
            return None
        try:
            expires_at = float(document.get("expires_at", 0))
        except (TypeError, ValueError):
            return None
        if expires_at <= self._now():
            _remove(path)
            return None
        return document.get("response")

    def set(
        self,
        kind: str,
        identifier: str,
        response: Any,
        *,
        source_url: str = "",
        params: dict[str, object] | None = None,
    ) -> None:
        if not self.enabled:
            return
        safe_kind = _safe_kind(kind)
        now = self._now()
        document = {
            "version": CACHE_VERSION,
            "kind": safe_kind,
            "identifier": identifier,
            "source_url": _safe_url(source_url),
            "params": _safe_mapping(params),
            "cached_at": now,
            "expires_at": now + self.ttl_seconds,
            "response": response,
        }
        path = self._path(safe_kind, identifier, params)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _entries_matching(
        self,
        *,
        kind: str | None = None,
        identifier: str | None = None,
    ) -> list[Path]:
        paths: list[Path] = []
        for path in self._entries.glob("*.json"):
            document = self._read(path)
            if document is None:
                continue
            if kind is not None and document.get("kind") != kind:
                continue
            if identifier is not None and document.get("identifier") != identifier:
                continue
            paths.append(path)
        return paths

    def invalidate(
        self,
        *,
        kind: str | None = None,
        identifier: str | None = None,
    ) -> int:
        """Remove matching entries and return the number deleted.

        With neither filter this is equivalent to :meth:`clear`. A kind filter
        is validated before any deletion so a malformed name cannot silently
        match nothing.
        """
        if kind is None and identifier is None:
            return self.clear()
        safe_kind = _safe_kind(kind) if kind is not None else None
        removed = 0
        for path in self._entries_matching(
            kind=safe_kind, identifier=identifier
        ):
            if _remove(path):
                removed += 1
        return removed

    def clear(self) -> int:
        """Remove every cache entry and return the number deleted."""
        removed = 0
        for path in self._entries.glob("*.json"):
            if _remove(path):
                removed += 1
        return removed

    def prune_expired(self) -> int:
        """Remove expired entries and return the number deleted."""
        now = self._now()
        removed = 0
        for path in self._entries.glob("*.json"):
            document = self._read(path)
            if document is None:
                continue
            try:
                expired = float(document.get("expires_at", 0)) <= now
            except (TypeError, ValueError):
                expired = True
            if not expired:
                continue
            if _remove(path):
                removed += 1
        return removed
