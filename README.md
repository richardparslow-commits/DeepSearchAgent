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
- Ranks results with an explainable ranking layer: court-authority tiers (Supreme Court > Federal Circuit > CAVC > BVA) are strictly dominant, and within a tier cases are scored by issue relevance, decision recency, and extracted-detail completeness.
- Fetches the top cases' source pages (HTML or PDF) to populate citation, decision date, holding, docket number, judge attribution, cited statutes, and procedural outcome where they can be extracted.
- Analyzes each case's impact with a nuanced profile: claim topics touched, procedural posture, statutory anchors, and weight of authority.
- Converts findings into structured VA claims guidance via the interpretive analysis layer: detected legal elements, case-backed principles, strengths/gaps, a coverage score, and actionable next steps. The narrative can be enhanced by OpenAI/Azure OpenAI when `OPENAI_API_KEY` is set; otherwise a deterministic template is used.
- Produces a structured analysis suitable for legal research and issue-spotting.

## How case ranking works

The ranking layer (`va_legal_agent/ranking.py`) orders candidates in two steps:

1. **Authority tiers are strictly dominant.** A Federal Circuit decision always outranks any CAVC or BVA decision, and so on up the hierarchy, because binding authority matters more than signal scores.
2. **Within a tier, a weighted composite score decides:**
   - relevance (60%): issue keyword relevance, normalized against the best match in the batch
   - recency (25%): decision year scaled from 1985 to today; unknown dates get a neutral score
   - completeness (15%): share of citation/decision date/holding extracted during enrichment

Each case carries a `composite_score` (authority tier + within-tier score) and a `ranking_explanation` describing the factor breakdown, and the analysis summary reports the score alongside authority and relevance.

## Case detail extraction

The enrichment step (`va_legal_agent/fetch.py`) pulls structured details from each top case's source page or PDF opinion:

- **Citation** — reporter citations (Vet.App., F.2d/F.3d/F.4th, U.S., S. Ct., Westlaw) and BVA citation numbers
- **Decision date** — ISO date, preferring dates marked "Decided"/"Filed"/"Issued"
- **Holding** — the first explicit holding sentence ("we hold that...", "the Court holds that..."), falling back to the page's meta description for HTML
- **Docket** — docket/case/appeal numbers (e.g. `19-4433`)
- **Judge** — judge attribution ("Judge Mary J. Smith", "Chief Judge ...") or "Per Curiam"
- **Statutes** — unique cited VA statutes, normalized (e.g. `38 U.S.C. § 5107(b)`, `38 C.F.R. § 3.303`)
- **Outcome** — procedural posture detected from disposition language (e.g. `vacated and remanded`)

All fields are best-effort: unparseable pages leave fields empty rather than failing the research run.

## Impact analysis layer

`va_legal_agent/impact.py` turns each case record into an `ImpactProfile` with a nuanced narrative (`agent.summarize_case_impact` delegates to it):

- **Issue tags** — claim topics the ruling touches (service connection, benefit of the doubt, reasons and bases, evidence evaluation, nexus, rating), in priority order, falling back to the claim issue itself.
- **Procedural posture** — the outcome (from enrichment, or detected from the title/holding) plus a nuance note explaining what vacatur, remand, affirmance, etc. can mean for a claim.
- **Statutory anchors** — statutes the ruling engages, mapped to doctrine hints (e.g. § 5107(b) benefit-of-the-doubt, § 7104(d)(1) reasons-and-bases).
- **Weight of authority** — binding appellate precedent vs. persuasive Board-level decisions vs. unidentified sources.

Output is deterministic research support derived from public decisions, not legal advice.

## Interpretive analysis layer

`va_legal_agent/interpretation.py` turns the ranked cases plus the claim issue into structured VA claims guidance:

- **Detected elements** — legal elements mentioned in the issue (service connection, nexus, benefit of the doubt, reasons and bases, presumptions, rating, aggravation, duty to assist, evidence evaluation), each with claimant guidance; `covered_by` lists which retrieved cases address the element.
- **Case-backed principles** — known veterans-law principles (e.g. 38 U.S.C. § 5107(b) benefit of the doubt, § 7104(d)(1) reasons and bases, nexus, evidence rules) scanned out of the case text with source-case attribution.
- **Strengths, gaps, and coverage score** — the share of detected elements that retrieved authority actually covers; gaps suggest where targeted research is needed.
- **Actionable next steps** — derived from the detected elements rather than static boilerplate.
- **Narrative** (`how_it_affects_va_claims`) — LLM-enhanced when `OPENAI_API_KEY` is set (`interpretation_source: "llm"`), deterministic template otherwise (`"template"`). All output states it is research support, not legal advice.

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
