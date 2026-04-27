# Author Lead Scraper

Python 3.12+ pipeline that discovers Amazon book listings (Playwright + [playwright-stealth](https://pypi.org/project/playwright-stealth/)), enriches authors with [LinkdAPI](https://linkdapi.com), validates and deduplicates leads, then scores and exports the top matches to `data/leads_final.csv`.

## Setup

1. Clone or copy this repository.
2. Create a virtual environment and install dependencies:

   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Copy `.env.example` to `.env` and set `LINKDAPI_KEY` ([LinkdAPI](https://linkdapi.com) — free credits on signup). Stage 1 (Amazon only) and optional Stage 1.5 (public email discovery) do not need this key; stages 2–4 do.

4. Tune `config/config.yaml` (`amazon_scraper` delays/viewport, pipeline thresholds, categories, scoring).

## Usage

Full pipeline (checkpoints under `data/`, logs under `logs/`):

```bash
python -m src.main --all
```

Run a single stage (earlier stages load from checkpoint when present):

```bash
python -m src.main --stage 2
python -m src.main --stage 2.5
python -m src.main --stage 4 --stage4-use-stage25-input
```

Run multiple stages in one invocation (comma-separated, executed in pipeline order):

```bash
python -m src.main --stage 1,1.5,2
```

### Stage 1.5 — public email enrichment (optional)

After Stage 1, Stage 1.5 crawls each book’s Amazon/author pages and optional DuckDuckGo results to find **publicly published** emails, then writes them onto the same `AmazonBook` JSON (`scraped_public_email`, `scraped_email_status`, etc.). Stage 2 prefers LinkdAPI contact info when present, otherwise falls back to these fields.

- Configure under `email_enrichment` in `config/config.yaml` (delays, concurrency, toggles). Set `enabled: false` to skip.
- **Not** included in `--all` by default (adds significant runtime and outbound requests). Run explicitly as shown above.
- Checkpoint: `data/<stem>.email_meta.json` (fingerprint of Stage 1 file + email settings). Use `--stage 1.5 --force` to re-run discovery.
- **Domain guessing (`use_domain_guess`):** If there is no **personal** address on crawled pages (or only generic inboxes like `info@`), the pipeline may build common `local@author-domain` patterns and require a DuckDuckGo snippet hit before keeping a candidate. Those rows are marked **`unverified`** with reason `ddg_guess_hit+mx` or `ddg_guess_hit+no_mx` — not proof the mailbox exists or belongs to the author; use only where appropriate.
- **Ethics / compliance:** Use only for legitimate outreach; respect site terms and `robots.txt`; this stage does not bypass logins or paywalls. It performs DNS MX checks only (no SMTP “ping”).

Recommended stage flows:

- Standard validated path: `2 -> 3 -> 4`
- Fast scoring path (skip stage 3 validation): `2 -> 2.5 -> 4 --stage4-use-stage25-input`

Force re-run a stage and invalidate downstream artifacts:

```bash
python -m src.main --stage 1 --force
```

### Enrich a standalone author CSV (LinkdAPI)

If you have `data/amazon_books_titles_authors.csv` (`asin`, `book_title`, `authors`), run LinkdAPI enrichment to `data/amazon_books_linkedin.csv`:

```bash
python -m src.csv_linkedin --dry-run
python -m src.csv_linkedin --force
```

Requires `LINKDAPI_KEY`. Use `--force` if an older output file lacks columns `urn`, `headline`, `email`, and `website`.

## Outputs

| Artifact | Description |
|----------|-------------|
| `data/amazon_raw.json` (config: `amazon_scraper.raw_books_json`) | Stage 1 — parsed `AmazonBook` rows (Stage 1.5 may add `scraped_*` fields in place) |
| `data/<stem>.meta.json` (e.g. `amazon_raw.meta.json`) | Stage 1 — fingerprint of search/filter config; if you change `config.yaml` and this no longer matches, Stage 1 re-scrapes automatically (unless you only have an old JSON with no meta — then it re-scrapes once). `--force` always re-scrapes. |
| `data/<stem>.email_meta.json` (e.g. `amazon_raw.email_meta.json`) | Stage 1.5 — fingerprint of `email_enrichment` settings + Stage 1 JSON bytes; mismatch triggers re-enrichment (unless `--force`). |
| `data/linkedin_matched.json` | Stage 2 — `EnrichedLead` rows |
| `data/verified_leads_stage25.json` | Stage 2.5 — compiled `VerifiedLead`-shape rows from Stage 2 (no re-validation, no dedupe) |
| `data/scoring_input_stage25.csv` | Stage 2.5 — flat scoring-ready CSV compiled from Stage 2 fields |
| `data/verified_leads.json` | Stage 3 — deduped `VerifiedLead` rows |
| `data/leads_final.csv` | Stage 4 — up to 10 ranked leads |
## Stage 2.5 vs Stage 4

- Stage 2.5 **does not** produce final ranking output. It prepares scoring input documents only.
- Stage 4 computes final `score` and `confidence_score`, applies min-score filtering, ranks candidates, and writes `data/leads_final.csv`.
- If you run Stage 4 with `--stage4-use-stage25-input`, Stage 4 reads `data/verified_leads_stage25.json` instead of `data/verified_leads.json`.

Examples:

```bash
# Build stage 2.5 inputs only
python -m src.main --stage 2.5 --force

# Final scoring from stage 2.5 input
python -m src.main --stage 4 --stage4-use-stage25-input --force

# Final scoring from validated stage 3 input
python -m src.main --stage 4 --force
```

| `data/amazon_books_linkedin.csv` | Optional — `python -m src.csv_linkedin` (LinkdAPI usernames from `amazon_books_titles_authors.csv`) |
| `logs/run_*.log` | Structured log for each invocation |
| `logs/summary_*.html` | HTML summary with counts and discard reasons |

## Runtime

Expect roughly 25–45 minutes end-to-end on a full run, depending on `amazon_scraper` delays, pagination, and how many product pages Stage 1 opens.

## Risks

Amazon may change HTML or increase friction. If you see captchas or empty parses, try non-headless (`amazon_scraper.use_headless: false`), increase `delay_between_pages`, or run from a residential network. Stage 3 opens a short Chromium session per product URL during validation.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) (v1.2 — Playwright) for the full pipeline spec.
