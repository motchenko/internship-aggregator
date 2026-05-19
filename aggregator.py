"""
Internship Aggregator
---------------------
Pulls Summer 2027 internship listings from community-maintained GitHub repos,
filters by quant/SWE/research keywords, scores by relevance, and appends new
postings to a Google Sheet 'Pipeline' tab.

Designed to run daily via GitHub Actions.

Environment variables required:
  GSHEETS_SERVICE_ACCOUNT_JSON  Base64-encoded service account credentials JSON
  GSHEET_ID                     Target Google Sheet ID (the /d/<id>/ part of URL)

Local dev:
  export GSHEETS_SERVICE_ACCOUNT_JSON=$(base64 -i credentials.json)
  export GSHEET_ID=1abc...
  python aggregator.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import requests
import gspread
from google.oauth2.service_account import Credentials


# ---------- Configuration ----------

REPOS = [
    {
        "name": "SimplifyJobs/Summer2027-Internships",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md",
        "fallback_url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    },
    {
        "name": "vanshb03/Summer2027-Internships",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/README.md",
        "fallback_url": "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/README.md",
    },
    {
        "name": "northwesternfintech/2027QuantInternships",
        "url": "https://raw.githubusercontent.com/northwesternfintech/2027QuantInternships/main/README.md",
        "fallback_url": "https://raw.githubusercontent.com/northwesternfintech/2026QuantInternships/main/README.md",
    },
]

# Higher score = more relevant. Sum of matched keyword scores = relevance score.
KEYWORDS = {
    # Quant — highest priority
    "quant": 10, "quantitative": 10, "trader": 8, "trading": 6,
    "market maker": 8, "market making": 8, "hft": 8, "high frequency": 8,
    # Quant research / dev
    "researcher": 6, "research": 4, "strategist": 5, "strats": 5,
    "alpha": 5, "signal": 4,
    # SWE / dev
    "software engineer": 4, "swe": 4, "developer": 3, "engineer": 2,
    # Sophomore-friendly
    "sophomore": 8, "discovery": 6, "academy": 5, "fellowship": 5,
    "insight": 4, "early": 3,
    # Locations (slight boost)
    "new york": 2, "nyc": 2, "chicago": 2, "london": 1, "amsterdam": 1,
}

# Firms whose listings auto-boost
PRIORITY_FIRMS = {
    "jane street": 15, "citadel": 15, "two sigma": 15, "hudson river": 15,
    "hrt": 15, "de shaw": 12, "d. e. shaw": 12, "optiver": 12,
    "jump trading": 12, "imc": 12, "drw": 10, "susquehanna": 12, "sig": 8,
    "five rings": 10, "akuna": 8, "tower research": 8, "millennium": 8,
    "point72": 8, "cubist": 8, "aqr": 8, "squarepoint": 8, "pdt": 8,
    "xtx": 8, "g-research": 8, "renaissance": 10, "bridgewater": 8,
    "quantlab": 6, "voleon": 6, "flow traders": 6,
    "goldman": 6, "morgan stanley": 5, "jpmorgan": 5, "jp morgan": 5,
}

# Sponsorship signals (rough — community repos don't always tag this)
NO_SPONSOR_PATTERNS = [
    "no sponsorship", "us citizen only", "u.s. citizen", "citizenship required",
    "security clearance", "must be authorized", "no visa",
]
SPONSOR_PATTERNS = ["sponsors visa", "visa sponsorship", "h1b", "h-1b", "cpt", "opt"]

# Min relevance score to include in output (raise to be pickier)
MIN_SCORE = 6


# ---------- Data classes ----------

@dataclass
class Posting:
    company: str
    role: str
    location: str
    apply_url: str
    source_repo: str
    sponsorship: str = "Unknown"
    relevance_score: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    raw_line: str = ""

    def to_row(self) -> list:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return [
            today, self.source_repo, self.company, self.role, self.location,
            self.apply_url, self.sponsorship, self.relevance_score, "No", "",
        ]

    def dedup_key(self) -> str:
        return f"{self.company.lower().strip()}|{self.role.lower().strip()}"


# ---------- Parsers ----------

def fetch_readme(repo: dict) -> str:
    """Try primary URL, fall back to previous-year URL if 2027 not yet live."""
    for url_key in ("url", "fallback_url"):
        url = repo.get(url_key)
        if not url:
            continue
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and len(r.text) > 1000:
                print(f"  Fetched: {url}")
                return r.text
        except Exception as e:
            print(f"  Failed {url}: {e}", file=sys.stderr)
    return ""


def parse_markdown_table(text: str, source_name: str) -> list[Posting]:
    """
    Parses markdown table rows like:
    | Company | Role | Location | URL | Date |
    | Jane Street | Quant Trader Intern | NYC | https://... | Dec 10 |

    Handles variations across repos (column order may differ slightly).
    """
    postings = []
    lines = text.split("\n")
    in_table = False
    headers = []

    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            in_table = False
            headers = []
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]

        # Header row
        if not in_table:
            if any(h.lower() in {"company", "name", "role", "position"} for h in cells):
                headers = [c.lower() for c in cells]
                in_table = True
            continue

        # Separator row (|---|---|)
        if all(re.match(r"^[-:\s]+$", c) for c in cells):
            continue

        if len(cells) < 3:
            continue

        # Map columns — repos use different layouts
        col_map = {}
        for i, h in enumerate(headers):
            if "company" in h or "name" in h:
                col_map["company"] = i
            elif "role" in h or "position" in h or "title" in h:
                col_map["role"] = i
            elif "location" in h:
                col_map["location"] = i
            elif "link" in h or "apply" in h or "url" in h:
                col_map["url"] = i

        if "company" not in col_map or "role" not in col_map:
            continue

        try:
            company = strip_markdown(cells[col_map["company"]])
            role = strip_markdown(cells[col_map["role"]])
            location = strip_markdown(cells[col_map.get("location", 0)]) if "location" in col_map else ""
            url = extract_url(cells[col_map.get("url", 0)]) if "url" in col_map else ""
        except (IndexError, KeyError):
            continue

        if not company or not role:
            continue
        if any(skip in role.lower() for skip in ["🔒", "closed", "🛑"]):
            continue

        p = Posting(
            company=company, role=role, location=location,
            apply_url=url, source_repo=source_name, raw_line=line,
        )
        score_posting(p)
        postings.append(p)

    return postings


def strip_markdown(s: str) -> str:
    """Remove markdown bolding, links, emoji."""
    s = re.sub(r"!\[.*?\]\(.*?\)", "", s)        # images
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # [text](url) -> text
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def extract_url(s: str) -> str:
    """Pull the first http(s) URL from a cell."""
    m = re.search(r"https?://[^\s)<>]+", s)
    return m.group(0) if m else ""


def score_posting(p: Posting) -> None:
    """Score relevance based on keyword + firm match. Also set sponsorship."""
    text = f"{p.company} {p.role} {p.location}".lower()
    score = 0
    matched = []

    for kw, weight in KEYWORDS.items():
        if kw in text:
            score += weight
            matched.append(kw)

    for firm, weight in PRIORITY_FIRMS.items():
        if firm in p.company.lower():
            score += weight
            matched.append(f"firm:{firm}")
            break  # Don't double-count multi-word firm matches

    p.relevance_score = score
    p.matched_keywords = matched

    # Sponsorship heuristic
    if any(pat in text for pat in NO_SPONSOR_PATTERNS):
        p.sponsorship = "No"
    elif any(pat in text for pat in SPONSOR_PATTERNS):
        p.sponsorship = "Likely"
    else:
        p.sponsorship = "Unknown"


# ---------- Google Sheets ----------

def get_sheet():
    """Authenticate and return the Pipeline worksheet."""
    sa_json_b64 = os.environ.get("GSHEETS_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GSHEET_ID")
    if not sa_json_b64 or not sheet_id:
        raise RuntimeError("Set GSHEETS_SERVICE_ACCOUNT_JSON and GSHEET_ID env vars.")

    creds_dict = json.loads(base64.b64decode(sa_json_b64).decode())
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    try:
        return sh.worksheet("Pipeline")
    except gspread.WorksheetNotFound:
        return sh.add_worksheet("Pipeline", rows=1000, cols=10)


def get_existing_keys(ws) -> set[str]:
    """Read existing rows from Pipeline to dedupe."""
    rows = ws.get_all_values()
    keys = set()
    for row in rows[1:]:
        if len(row) >= 4:
            keys.add(f"{row[2].lower().strip()}|{row[3].lower().strip()}")
    return keys


def append_postings(ws, postings: Iterable[Posting]) -> int:
    """Append new postings to Pipeline tab. Returns count appended."""
    rows = [p.to_row() for p in postings]
    if not rows:
        return 0
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


# ---------- Main ----------

def main() -> int:
    print(f"Aggregator run @ {datetime.now(timezone.utc).isoformat()}")

    all_postings: list[Posting] = []
    for repo in REPOS:
        print(f"\nFetching {repo['name']}...")
        text = fetch_readme(repo)
        if not text:
            print(f"  No content from {repo['name']}, skipping.")
            continue
        parsed = parse_markdown_table(text, repo["name"])
        print(f"  Parsed {len(parsed)} postings")
        all_postings.extend(parsed)

    # Filter by min score
    filtered = [p for p in all_postings if p.relevance_score >= MIN_SCORE]
    print(f"\nTotal parsed: {len(all_postings)}; passing min score {MIN_SCORE}: {len(filtered)}")

    # Dedup against existing sheet
    try:
        ws = get_sheet()
    except Exception as e:
        print(f"Sheets error: {e}", file=sys.stderr)
        # In dry-run / local mode, print top results and exit
        filtered.sort(key=lambda p: -p.relevance_score)
        print("\nTop 20 (dry run):")
        for p in filtered[:20]:
            print(f"  [{p.relevance_score:3d}] {p.company} | {p.role} | {p.location}")
        return 0

    existing = get_existing_keys(ws)
    new_postings = [p for p in filtered if p.dedup_key() not in existing]
    print(f"New (not yet in sheet): {len(new_postings)}")

    # Sort new by relevance score descending
    new_postings.sort(key=lambda p: -p.relevance_score)

    appended = append_postings(ws, new_postings)
    print(f"Appended {appended} rows to Pipeline tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
