# DeepSearchAgent

A focused legal research and case-analysis agent for U.S. veterans compensation claims.

## Purpose

This project is designed to search widely for relevant court and board decisions affecting veterans compensation cases, then explain the impact of those rulings on the VA claims process.

## Included authority sources

- U.S. Court of Veterans Appeals (CAVC)
- U.S. Court of Appeals for the Federal Circuit
- U.S. Supreme Court
- Board of Veterans' Appeals (BVA)

## Quick start

1. Create a virtual environment.
2. Install dependencies (use `requirements-dev.txt` instead if you also want the test/dev tooling):
   ```bash
   pip install -r requirements.txt
   ```
3. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
4. Run the agent:
   ```bash
   python -m va_legal_agent "service connection for tinnitus"
   ```

## Running tests

```bash
python -m pytest -q
```

## Example usage

```python
from va_legal_agent.agent import analyze_cases_for_claim

analysis = analyze_cases_for_claim("service connection for tinnitus", max_results=5)
print(analysis.model_dump_json(indent=2))
```

## What this agent does

- Searches across the major veterans law sources: CAVC, Federal Circuit, Supreme Court, and BVA.
- Finds candidate cases based on claim issue, benefit type, and legal topic.
- Summarizes how a case affects a VA compensation claim and what legal principles are likely relevant.
- Produces a structured analysis suitable for legal research and issue-spotting.

## Notes

This is a strong research foundation for legal analysis. For production use, a paid legal database or a more structured case-indexing layer can improve completeness and precision.

Web search is performed against DuckDuckGo's HTML endpoint without an API key; heavy use may be rate-limited or blocked by the provider. If every query fails, the agent raises a `SearchError` describing the last underlying error. Request timeout can be tuned via `REQUEST_TIMEOUT_SECONDS` in `.env`.
