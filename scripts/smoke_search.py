"""Smoke-test each configured search provider against the live network.

Runs one real query per provider in ``SEARCH_PROVIDERS`` (default:
``duckduckgo``) and prints a per-provider summary: how many results came back,
the first few titles and URLs, and any failures. Intended for manual network
sanity checks — CI cannot do this because the providers rate-limit anonymous
traffic, so run it periodically to confirm recall still works.

Usage:
    python scripts/smoke_search.py [QUERY]
    SEARCH_PROVIDERS="duckduckgo,courtlistener,bva" python scripts/smoke_search.py

Exits non-zero if any provider failed, so ``make smoke`` reports the result.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Make the package importable even when this script runs from a checkout that
# hasn't been `pip install -e`'d (running a script puts scripts/ on sys.path,
# not the project root).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from va_legal_agent.config import get_settings  # noqa: E402 - after path shim
from va_legal_agent.providers import (  # noqa: E402
    get_provider,
    validate_search_providers,
)


def main() -> int:
    settings = get_settings()
    names = validate_search_providers(settings.search_providers)
    if not names:
        print("No providers to smoke-test; check SEARCH_PROVIDERS.", file=sys.stderr)
        return 1

    query = sys.argv[1] if len(sys.argv) > 1 else "service connection for tinnitus"
    failures = 0
    for name in names:
        try:
            results = get_provider(name).search(query, max_results=3)
        except Exception as exc:  # noqa: BLE001 - the smoke test reports, not crashes
            print(f"[{name}] FAILED: {exc}")
            failures += 1
            continue
        print(f"[{name}] {len(results)} result(s) for {query!r}")
        for result in results[:2]:
            print(f"  - {result.get('title', '')!r}  {result.get('url', '')}")
        time.sleep(settings.search_delay_seconds)

    if failures:
        print(f"{failures} provider(s) failed; see messages above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
