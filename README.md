# Author Lead Scraper

Python 3.12+ pipeline that discovers Amazon book listings (Playwright + [playwright-stealth](https://pypi.org/project/playwright-stealth/)), enriches authors with [LinkdAPI](https://linkdapi.com), validates and deduplicates leads, then scores and exports the top matches to `data/leads_final.csv`.

## Setup

1. Clone or copy this repository.
2. Create a virtual environment and install dependencies:

   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Copy `.env.example` to `.env` and set `LINKDAPI_KEY` ([LinkdAPI](https://linkdapi.com) — free credits on signup). Stage 1 (Amazon only) does not need this key; stages 2–4 do.

4. Tune `config/config.yaml` (`amazon_scraper` delays/viewport, pipeline thresholds, categories, scoring).

## Usage

Full pipeline (checkpoints under `data/`, logs under `logs/`):

```bash
python -m src.main --all
```

Run a single stage (earlier stages load from checkpoint when present):

```bash
python -m src.main --stage 2
```

Force re-run a stage and invalidate downstream artifacts:

```bash
python -m src.main --stage 1 --force
```

## Outputs

| Artifact | Description |
|----------|-------------|
| `data/amazon_raw.json` | Stage 1 — parsed `AmazonBook` rows |
| `data/linkedin_matched.json` | Stage 2 — `EnrichedLead` rows |
| `data/verified_leads.json` | Stage 3 — deduped `VerifiedLead` rows |
| `data/leads_final.csv` | Stage 4 — up to 10 ranked leads |
| `logs/run_*.log` | Structured log for each invocation |
| `logs/summary_*.html` | HTML summary with counts and discard reasons |

## Runtime

Expect roughly 25–45 minutes end-to-end on a full run, depending on `amazon_scraper` delays, pagination, and how many product pages Stage 1 opens.

## Risks

Amazon may change HTML or increase friction. If you see captchas or empty parses, try non-headless (`amazon_scraper.use_headless: false`), increase `delay_between_pages`, or run from a residential network. Stage 3 opens a short Chromium session per product URL during validation.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) (v1.2 — Playwright) for the full pipeline spec.
