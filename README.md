# Author Lead Scraper

Python 3.12+ pipeline that discovers Amazon book listings (Playwright + [playwright-stealth](https://pypi.org/project/playwright-stealth/)), enriches authors with [LinkdAPI](https://linkdapi.com), validates and deduplicates leads, then scores and exports the top matches to `data/leads_final.csv`.

## Setup

1. Clone or copy this repository.
2. Create a virtual environment and install dependencies:

   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Copy `.env.example` to `.env` and set `LINKDAPI_KEY` ([LinkdAPI](https://linkdapi.com) — free credits on signup). Stage 1 does not need this key. Stages **2**, **2.5**, **3**, and **4** require it when those stages run. Stage **2.1** only reads `linkedin_matched.json` and does not call LinkdAPI (but you normally run it after stage 2).

4. Tune `config/config.yaml` (`amazon_scraper` delays/viewport, pipeline thresholds, categories, scoring).

## Usage

Primary run order (recommended):

1) **Stage 1** — Amazon book scrape  
2) **Stage 2** — LinkedIn enrichment  
3) **Stage 2.1** — LinkedIn-aware email enrichment  
4) **Stage 2.5** — compile scoring-ready artifacts

```bash
python -m src.main --stage 1 --force
python -m src.main --stage 2 --force
python -m src.main --stage 2.1 --force
python -m src.main --stage 2.5 --force
```

Or run those four stages in one command:

```bash
python -m src.main --stage 1,2,2.1,2.5 --force
```

> Every other stage is optional (see below).

### Stage 2.1 — LinkedIn-aware email enrichment (optional)

After Stage 2, Stage 2.1 reads **`data/linkedin_matched.json`**, derives candidate mail domains from LinkedIn profile data (job-description emails, company slug + MX heuristics), optionally uses LinkdAPI `contactInfo` when present, runs **personal local-part guesses** and the **Truth Reactor** (DNS/SMTP), then updates each lead’s **`contact_email`**, **`contact_website`**, nested **`book.scraped_*`** fields, and **`notes`**.

- Configure under `email_enrichment` in `config/config.yaml`. Set `enabled: false` to skip. **`stage21_max_smtp_probes_per_lead`** caps SMTP checks per author (guesses may be longer).
- Included in **`--all`** after stage 2 (can add significant runtime when SMTP is enabled).
- Checkpoint: **`data/linkedin_matched.stage21_meta.json`** (fingerprint of `linkedin_matched.json` + email settings). Use `--stage 2.1 --force` to re-run.
- **Ethics / compliance:** Use only for legitimate outreach; SMTP verification sends real RCPT probes — use appropriate timeouts and volume.

Optional stages:

- **Stage 3** — extra validation + dedupe (`data/verified_leads.json`)
- **Stage 4** — final scoring/ranking (`data/leads_final.csv`)

Optional stage flows:

- Standard validated path: `1 -> 2 -> 2.1 -> 3 -> 4`
- Fast scoring path (skip stage 3 validation): `1 -> 2 -> 2.1 -> 2.5 -> 4 --stage4-use-stage25-input`

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
| `data/amazon_raw.json` (config: `amazon_scraper.raw_books_json`) | Stage 1 — parsed `AmazonBook` rows |
| `data/<stem>.meta.json` (e.g. `amazon_raw.meta.json`) | Stage 1 — fingerprint of search/filter config; if you change `config.yaml` and this no longer matches, Stage 1 re-scrapes automatically (unless you only have an old JSON with no meta — then it re-scrapes once). `--force` always re-scrapes. |
| `data/linkedin_matched.json` | Stage 2 — `EnrichedLead` rows (Stage 2.1 updates contact fields in place) |
| `data/linkedin_matched.stage21_meta.json` | Stage 2.1 — fingerprint of `email_enrichment` + `linkedin_matched.json` bytes |
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
