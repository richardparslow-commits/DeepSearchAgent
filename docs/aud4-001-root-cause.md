# AUD4-001 — GovInfo/Congress API key disclosure

**Status:** Closed — verified by the regression suite and a mocked 401 reproduction.

**Affected release:** commit `c4c8ba52f84b655619937e8b8573a96ce5171dac`

**Severity:** Critical (credential disclosure in errors and tracebacks)

**Original locations:** `va_legal_agent/authorities.py:L856-L867`
(`GovInfoClient.search`), `va_legal_agent/congress.py:L278-L280`
(`CongressClient._request_json`, reached by `_govinfo_search` and
`_govinfo_summary`), `va_legal_agent/__main__.py:L157-L185` (failure event and
traceback formatting), and `webapp.py:L241-L251` (job error persistence).

## 1. Original data flow

### 1.1 GovInfo query-string injection

`GovInfoClient.search` sent the key as a request query parameter:

```python
response = requests.post(
    GOVINFO_SEARCH_URL,
    params={"api_key": self.api_key},
    json=body,
    headers=_headers(),
    ...
)
```

The same pattern existed in `CongressClient._govinfo_search` and
`CongressClient._govinfo_summary`. `CongressClient._congress_get` already used
the `X-Api-Key` header; `RegulationsClient` and `CourtListenerProvider` also
used headers. GovInfo was therefore the only production code path that placed
a configured API key in a URL query string.

### 1.2 Additional call sites

A repository-wide search found GovInfo key injection in exactly three request
builders: `GovInfoClient.search`, `CongressClient._govinfo_search`, and
`CongressClient._govinfo_summary`. Congress.gov bill/CRS requests already used
`headers={"X-Api-Key": self.api_key}`; `RegulationsClient` already used
`X-Api-Key`; `CourtListenerProvider` already used
`Authorization: Token ...`. No other production request put a configured API
key in a URL query string.

### 1.3 Entry into the exception message

`response.raise_for_status()` raises `requests.HTTPError`. `requests` constructs
that message from the response URL, for example:

```text
401 Client Error: Unauthorized for url:
https://api.govinfo.gov/search?api_key=SUPERSECRET123
```

`GovInfoClient` then formatted the exception directly:

```python
raise SearchError(f"GovInfo search failed: {exc}") from exc
```

`CongressClient` had a small `api_key=` regex, but the raw exception was still
chained with `from exc`; the regex did not scrub the cause, its attributes, or
literal key values that appeared outside the query string.

### 1.4 Preservation in the chain

`raise SearchError(...) from exc` sets `SearchError.__cause__` to the raw
`HTTPError` and leaves the raw exception in `__context__`. The `HTTPError`
keeps the unredacted URL in its `args`, and when available in
`response.url` / `request.url`. Formatting the chain therefore emitted the
key even if the outer message happened to be safe:

```python
traceback.format_exception(type(error), error, error.__traceback__)
```

### 1.5 CLI exposure

`va_legal_agent/__main__.py` rendered `_format_exception(error)` into per-issue
error rows and the `analysis_failed` event. `_JsonFormatter` also used
`self.formatException(record.exc_info)`. Neither boundary sanitized the chain.
`main()` re-raised the same exception after emitting the event, so Python's
unhandled-exception traceback could expose the raw cause as well.

### 1.6 Web exposure

`webapp._run_job` caught the exception and stored `f"{type(exc).__name__}: {exc}"`
in the persisted job. The status and runs endpoints returned that value, and the
job record survived server restarts. No boundary redaction existed there.

## 2. Remediation

### Layer 1 — centralized redaction

Added `va_legal_agent/redaction.py`:

- `redact_secrets(text, extra_secrets=...)` removes configured literal keys,
  their URL-encoded forms, sensitive query pairs (including URL-encoded names
  such as `api%5Fkey`), and URL userinfo.
- `redact_url(url, extra_secrets=...)` parses the URL, redacts sensitive query
  values and userinfo, then applies literal-key redaction.
- `sanitize_exception(exc, extra_secrets=...)` mutates `args`, `url`,
  `filename`, `msg`, `reason`, exception notes, attached request/response URLs
  and sensitive headers, and recursively sanitizes `__cause__`/`__context__`.
  The recursion uses an identity set so cyclic chains terminate.

`SearchError` now redacts its own message at construction. This is a safety net
for every provider, not a replacement for boundary sanitization.

### Layer 2 — header authentication and sanitized causes

`authorities.py` and `congress.py` now send GovInfo credentials in
`X-Api-Key`, matching api.data.gov's header authentication and removing the key
from request URLs. Both clients call `sanitize_exception` and
`redact_secrets` before raising `SearchError`. `regulations.py` applies the
same sanitization even though it already used a header. `_fetch_govinfo_text`
also sanitizes its chained request exception.

### Layer 3 — output boundaries

`__main__.py` now:

- sanitizes the exception chain before `traceback.format_exception`;
- redacts failure event messages, traceback fields, batch rows, and tracker
  records;
- uses redacting text and JSON log formatters, including sanitizing
  `exc_info` before `formatException`;
- sanitizes the exception before the `main()` re-raise.

`webapp.py` sanitizes and redacts before persisting an error, and re-redacts
job errors when loading old state and when serving status/runs responses.

### Layer 4 — regression coverage

- `tests/test_authorities.py` — GovInfo header auth and mocked 401 chain.
- `tests/test_congress.py` — GovInfo header auth, legacy query redaction, and
  mocked 401 chain.
- `tests/test_cli.py` — `_format_exception`, failure event, JSON formatter, and
  captured stderr/stdout.
- `tests/test_webapp.py` — persisted job error and status response.
- `tests/audit_invariants.py` / `tests/test_audit_invariants.py` — independent
  chain walker and negative test.
- `tests/test_redaction.py` — literal, encoded, URL, userinfo, request,
  response, notes, and cyclic-chain cases.

## 3. Verification

```text
.venv/bin/python -m pytest tests/test_authorities.py tests/test_congress.py \
  tests/test_cli.py tests/test_webapp.py tests/test_audit_invariants.py \
  tests/test_redaction.py -q
323 passed

.venv/bin/python -m ruff check va_legal_agent tests webapp.py
All checks passed!

.venv/bin/python -m pytest -q
2278 passed
```

The 401 tests assert that `SUPERSECRET123` is absent from `str(exc)`,
`repr(exc)`, each exception's `args`, each `__cause__`/`__context__`, the
formatted traceback, CLI stdout/stderr, and the web status response.
