# Internship Aggregator

Pulls Summer 2027 quant/SWE internship postings from community-maintained
GitHub repos and appends new entries to a Google Sheets pipeline tab.
Runs daily via GitHub Actions.

**Sources pulled:**
- `SimplifyJobs/Summer2027-Internships`
- `vanshb03/Summer2027-Internships`
- `northwesternfintech/2027QuantInternships`

Falls back to Summer 2026 repos automatically if 2027 repos aren't live yet.

## Setup

### 1. Create a Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a new project (e.g., "internship-aggregator").
3. Enable the **Google Sheets API** (APIs & Services → Library → search "Sheets").
4. Create a service account (IAM & Admin → Service Accounts → Create).
5. Generate a JSON key for the service account; download it as `credentials.json`.
6. Open your tracker Google Sheet → Share → paste the service account's email
   (looks like `xxx@yyy.iam.gserviceaccount.com`) with **Editor** access.

### 2. Set GitHub secrets

In your repo: Settings → Secrets and variables → Actions → New repository secret.

| Secret | Value |
|---|---|
| `GSHEETS_SERVICE_ACCOUNT_JSON` | base64-encoded contents of `credentials.json`. Generate with `base64 -i credentials.json \| pbcopy` (Mac) or `base64 -w 0 credentials.json` (Linux). |
| `GSHEET_ID` | The ID portion of your sheet's URL: `docs.google.com/spreadsheets/d/<THIS_PART>/edit`. |

### 3. Move the workflow file

Move `aggregator.yml` to `.github/workflows/aggregator.yml` in your repo.

### 4. Test locally first

```bash
pip install -r requirements.txt
export GSHEETS_SERVICE_ACCOUNT_JSON=$(base64 -i credentials.json)  # macOS
export GSHEET_ID=your_sheet_id_here
python aggregator.py
```

If the env vars aren't set, the script prints the top 20 results without
writing — useful for verifying parsing.

### 5. Manual run on GitHub

Repo → Actions tab → Internship Aggregator → Run workflow.

Then verify the Pipeline tab of your sheet got new rows.

## How it works

1. Fetches each source repo's README.md (raw GitHub URL).
2. Parses markdown tables, extracting Company / Role / Location / URL.
3. Scores each posting by keyword + firm match (see `KEYWORDS` and
   `PRIORITY_FIRMS` in `aggregator.py`).
4. Filters by minimum relevance score (default 6).
5. Dedupes against existing rows in the Pipeline tab.
6. Appends new rows, sorted by relevance score descending.

## Tuning

- **Want more results?** Lower `MIN_SCORE` in `aggregator.py`.
- **Want fewer?** Raise it.
- **Different firm priorities?** Edit `PRIORITY_FIRMS`.
- **New source repo?** Append to `REPOS`.

## Files

- `aggregator.py` — main script
- `requirements.txt` — Python deps
- `aggregator.yml` — GitHub Actions workflow (move to `.github/workflows/`)
- `README.md` — this file

## Resume bullet template (when this is running)

> Built a Python aggregator that scrapes 3 community-maintained internship
> repositories daily, scores postings by relevance to quant/SWE/research, and
> appends de-duplicated entries to a Google Sheets pipeline via the Sheets API.
> Deployed on GitHub Actions; processes ~1,500 listings/week into a curated
> 200-row top-relevance feed. Saved ~3 hours/week of manual triage.

## License

MIT — personal project, do whatever.
