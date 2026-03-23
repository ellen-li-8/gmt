import os, re, io, time, logging, uuid, threading
from flask import Flask, render_template, send_file, Response
import requests
import pdfplumber
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# 2-digit term years 2011–2025
TERM_YEARS = list(range(11, 26))


# ─── Step 1: Fetch granted/noted list PDF (1 per term) ────────────────────────

def fetch_granted_list_pdf(term_year: int) -> str:
    """
    Download the Granted/Noted Cases List PDF for a term and return its text.
    URL: https://www.supremecourt.gov/orders/NNgrantednotedlist.pdf
    """
    url = f"https://www.supremecourt.gov/orders/{term_year:02d}grantednotedlist.pdf"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Failed to fetch granted list for term {term_year}: {e}")
        return ""
    try:
        text = ""
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
        return text
    except Exception as e:
        log.warning(f"Failed to parse PDF for term {term_year}: {e}")
        return ""


# ─── Step 2: Parse cases from the granted/noted list PDF ──────────────────────

def parse_granted_list(text: str, term_year: int) -> list[dict]:
    """
    Parse the Granted/Noted Cases List PDF.
    Lines look like:
      11-1234   SMITH v. JONES
    """
    cases = []
    full_term = f"October {2000 + term_year}"

    docket_re = re.compile(r"^\s*(?:No\.\s*)?(\d{1,2}-\d{1,5})\s{2,}(.+)")
    skip_re = re.compile(
        r"(GRANTED|NOTED|CASE NAME|DOCKET|^\s*$|October Term|Page \d|"
        r"Supreme Court|Washington|^\s*\d+\s*$|Continued)",
        re.IGNORECASE
    )

    for line in text.split("\n"):
        if skip_re.search(line):
            continue
        m = docket_re.match(line)
        if m:
            docket = m.group(1).strip()
            case_name_raw = m.group(2).strip()
            # Strip trailing columns (date, etc.)
            case_name_raw = re.split(r"\s{3,}", case_name_raw)[0]
            case_name = clean_case_name(case_name_raw)
            if case_name:
                cases.append({
                    "docket": docket,
                    "case_name": case_name,
                    "date": "",
                    "term": full_term,
                    "granted_cert": 1,
                    "outcome": "",
                    "decision_direction": "",
                    "issue_area": "",
                    "oyez_url": "",
                    "circuit_split": 0,
                    "federalism": 0,
                    "precedent": 0,
                    "national_significance": 0,
                })
    return cases


def clean_case_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    name = re.sub(r"\s+\d+$", "", name).strip()
    if not re.search(r"\bv\.?\s", name, re.IGNORECASE):
        return ""
    return name[:200]


# ─── Step 3: Enrich with Oyez (one API call per term, no per-case fetches) ────

def enrich_with_oyez(cases: list[dict]) -> list[dict]:
    """
    One Oyez API call per term year to get issue_area, decision_direction,
    winning_party, decision_date. Much faster than per-case fetching.
    """
    by_term: dict[int, list] = {}
    for c in cases:
        yr_match = re.search(r"(\d{4})", c.get("term", ""))
        if yr_match:
            by_term.setdefault(int(yr_match.group(1)), []).append(c)

    for year, term_cases in sorted(by_term.items()):
        log.info(f"Fetching Oyez data for {year}...")
        oyez_list = fetch_oyez_term(year)

        lookup: dict[str, dict] = {}
        for oc in oyez_list:
            d = (oc.get("docket_number") or "").strip()
            if d:
                lookup[d] = oc
                lookup[re.sub(r"-0+(\d)", r"-\1", d)] = oc

        for c in term_cases:
            oc = lookup.get(c["docket"]) or lookup.get(
                re.sub(r"-0+(\d)", r"-\1", c["docket"])
            )
            if not oc:
                continue

            href = oc.get("href", "") or ""
            c["oyez_url"] = href.replace("api.oyez.org", "www.oyez.org")
            c["issue_area"] = oc.get("issue_area") or ""

            decisions = oc.get("decisions") or []
            if decisions:
                d0 = decisions[0]
                c["decision_direction"] = d0.get("decision_direction") or ""
                c["outcome"] = d0.get("winning_party") or ""

            dd = oc.get("decision_date")
            if dd and not c["date"]:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromtimestamp(int(dd), tz=timezone.utc)
                    c["date"] = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

        time.sleep(0.3)

    return cases


def fetch_oyez_term(year: int) -> list[dict]:
    url = f"https://api.oyez.org/cases?filter=term:{year}&per_page=0"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Oyez fetch failed for {year}: {e}")
        return []


# ─── Step 4: Build Excel ──────────────────────────────────────────────────────

def build_excel(cases: list[dict]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SCOTUS Cert Cases"

    headers = [
        "Case Name", "Docket #", "Date", "Term",
        "Granted Cert (0/1)", "Outcome / Winning Party",
        "Decision Direction", "Issue Area",
        "Circuit Split", "Federalism Conflict",
        "Precedent Matter", "National Significance",
        "Oyez URL",
    ]

    header_fill = PatternFill(start_color="1a1a2e", end_color="1a1a2e", fill_type="solid")
    header_font = Font(color="c8a96e", bold=True, name="Calibri", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="444444")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = border
    ws.row_dimensions[1].height = 36

    row_fill_even = PatternFill(start_color="f5f3ee", end_color="f5f3ee", fill_type="solid")
    row_font = Font(name="Calibri", size=10)
    center_align = Alignment(horizontal="center", vertical="top")
    left_align = Alignment(horizontal="left", vertical="top", wrap_text=True)

    for row_idx, c in enumerate(cases, 2):
        fill = row_fill_even if row_idx % 2 == 0 else None
        row_data = [
            c.get("case_name", ""),
            c.get("docket", ""),
            c.get("date", ""),
            c.get("term", ""),
            c.get("granted_cert", 1),
            c.get("outcome", ""),
            c.get("decision_direction", ""),
            c.get("issue_area", ""),
            c.get("circuit_split", 0),
            c.get("federalism", 0),
            c.get("precedent", 0),
            c.get("national_significance", 0),
            c.get("oyez_url", ""),
        ]
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = row_font
            cell.border = border
            if fill:
                cell.fill = fill
            cell.alignment = left_align if col_idx in (1, 6, 7, 8, 13) else center_align

    col_widths = [45, 12, 14, 18, 16, 28, 22, 24, 14, 20, 18, 22, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "SCOTUS Certiorari Dataset — Summary"
    ws2["A1"].font = Font(bold=True, size=14, name="Calibri")
    terms = sorted(set(c.get("term", "") for c in cases))
    from datetime import datetime
    for i, (label, val) in enumerate([
        ("Total cases (granted cert):", len(cases)),
        ("Terms covered:", ", ".join(terms)),
        ("Generated:", datetime.now().strftime("%Y-%m-%d %H:%M UTC")),
    ], 3):
        ws2.cell(row=i, column=1, value=label).font = Font(name="Calibri", size=10)
        ws2.cell(row=i, column=2, value=val).font = Font(name="Calibri", size=10)
    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 60

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ─── Job store + background worker ───────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _upd(job_id: str, **kw):
    _jobs[job_id].update(kw)


def run_scrape_job(job_id: str):
    all_cases = []
    try:
        for idx, term_year in enumerate(TERM_YEARS):
            full_year = 2000 + term_year
            pct = int((idx / len(TERM_YEARS)) * 70)
            _upd(job_id,
                 msg=f"Fetching granted list for {full_year} ({idx+1}/{len(TERM_YEARS)})...",
                 pct=pct)

            text = fetch_granted_list_pdf(term_year)
            if not text:
                _upd(job_id, msg=f"No data for {full_year}, skipping.", pct=pct)
                continue

            cases = parse_granted_list(text, term_year)

            seen: set[str] = set()
            unique = []
            for c in cases:
                if c["docket"] not in seen:
                    seen.add(c["docket"])
                    unique.append(c)

            all_cases.extend(unique)
            _upd(job_id, msg=f"{full_year}: {len(unique)} cases found.", pct=pct)
            time.sleep(0.1)

        _upd(job_id, msg=f"Enriching {len(all_cases)} cases with Oyez data...", pct=72)
        all_cases = enrich_with_oyez(all_cases)

        _upd(job_id, msg="Building Excel file...", pct=95)
        excel_bytes = build_excel(all_cases)

        _upd(job_id,
             status="done",
             msg=f"Done. {len(all_cases):,} granted cases across {len(TERM_YEARS)} terms.",
             pct=100,
             result_bytes=excel_bytes,
             preview=all_cases[:300],
             total=len(all_cases))

    except Exception as e:
        log.exception("Scrape job failed")
        _upd(job_id, status="error", msg=str(e))


# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start():
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running", "msg": "Starting...", "pct": 0}
    threading.Thread(target=run_scrape_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


@app.route("/api/status/<job_id>")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"error": "Job not found"}, 404
    return {
        "status":  job.get("status", "running"),
        "msg":     job.get("msg", ""),
        "pct":     job.get("pct", 0),
        "total":   job.get("total", 0),
        "preview": job.get("preview", []),
    }


@app.route("/api/download/<job_id>")
def download(job_id: str):
    job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        return {"error": "Job not ready"}, 404
    return send_file(
        io.BytesIO(job["result_bytes"]),
        download_name="scotus_cert_cases.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
