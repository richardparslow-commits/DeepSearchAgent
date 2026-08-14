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
   Alternatively, install as a package (also provides the `va-legal-agent` command):
   ```bash
   pip install -e .          # add "[llm]" for the optional OpenAI feature
   ```
3. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
4. Run the agent:
   ```bash
   python -m va_legal_agent "service connection for tinnitus"
   ```
   Use `--no-enrich` to skip fetching case source pages for citation/date details.

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

- Searches across the major veterans law sources: CAVC, Federal Circuit, Supreme Court, and BVA (queries run concurrently).
- Finds candidate cases based on claim issue, benefit type, and legal topic.
- Ranks results by court authority (Supreme Court > Federal Circuit > CAVC > BVA), then by issue relevance within each tier.
- Fetches the top cases' source pages (HTML or PDF) to populate citation, decision date, and holding details where they can be extracted.
- Optionally uses OpenAI/Azure OpenAI to interpret how the top cases affect the claim when `OPENAI_API_KEY` is set; otherwise it falls back to template-based summaries.
- Produces a structured analysis suitable for legal research and issue-spotting.

## Configuration

Environment variables (see `.env.example`; loaded automatically via python-dotenv):

| Variable | Default | Purpose |
|---|---|---|
| `REQUEST_TIMEOUT_SECONDS` | `20` | Timeout for each outbound search/fetch request |
| `SEARCH_MAX_WORKERS` | `4` | Number of court-site search queries run concurrently |
| `SEARCH_DELAY_SECONDS` | `0.5` | Stagger between starting consecutive search queries (`0` disables) |
| `OPENAI_API_KEY` | – | Enables optional LLM interpretation of the top cases |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used for LLM interpretation |
| `OPENAI_BASE_URL` | – | Endpoint override for Azure OpenAI / compatible APIs |

## Notes

This is a strong research foundation for legal analysis. For production use, a paid legal database or a more structured case-indexing layer can improve completeness and precision.

Web search is performed against DuckDuckGo's HTML endpoint without an API key; heavy use may be rate-limited or blocked by the provider. If every query fails, the agent raises a `SearchError` describing the last underlying error. Request timeout can be tuned via `REQUEST_TIMEOUT_SECONDS` in `.env`.
