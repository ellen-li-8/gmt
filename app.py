import os, re, io, time, logging, uuid, threading
from flask import Flask, render_template, send_file
import requests
from bs4 import BeautifulSoup
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

# 2-digit SCOTUS term years 2011–2025
TERM_YEARS = list(range(11, 26))


# ─── SCOTUS: get order list PDF URLs for a term ───────────────────────────────

def get_order_list_urls(term_year: int) -> list[dict]:
    """Return list of {url, date} for all Order List PDFs in a given term."""
    url = f"https://www.supremecourt.gov/orders/ordersofthecourt/{term_year:02d}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Failed to fetch order page for term {term_year}: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Order list PDFs contain "zor.pdf" in the filename
        if "zor.pdf" in href.lower():
            full_url = (
                href if href.startswith("http")
                else "https://www.supremecourt.gov" + href
            )
            # Date is in the surrounding text (e.g. "06/25/12")
            parent_text = a.parent.get_text(" ") if a.parent else ""
            date_m = re.search(r"(\d{2}/\d{2}/\d{2,4})", parent_text)
            date_str = date_m.group(1) if date_m else ""
            results.append({"url": full_url, "date": date_str})
    return results


# ─── Parse a single order list PDF ───────────────────────────────────────────

def parse_order_list_pdf(pdf_url: str, date_str: str, term_year: int) -> list[dict]:
    """Download a PDF and return granted-cert cases extracted from it."""
    try:
        r = requests.get(pdf_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Failed to fetch PDF {pdf_url}: {e}")
        return []

    try:
        full_text = ""
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    full_text += t + "\n"
    except Exception as e:
        log.warning(f"Failed to parse PDF {pdf_url}: {e}")
        return []

    return extract_granted_cases(full_text, date_str, term_year, pdf_url)


def extract_granted_cases(text: str, date_str: str, term_year: int, source_url: str) -> list[dict]:
    """
    Parse PDF text and extract only CERTIORARI GRANTED cases.

    Order list PDFs have sections like:
      CERTIORARI GRANTED
        11-338 ) DECKER, DOUG, ET AL. V. NORTHWEST ENVTL. DEFENSE CENTER
        11-347 ) GEORGIA-PACIFIC WEST, ET AL. V. ...
          The petitions for writs of certiorari are granted...
        11-460 LOS ANGELES CTY. FLOOD CONTROL V. NATURAL RESOURCES, ET AL.
          The petition for a writ of certiorari is granted...
      CERTIORARI DENIED
        ...  ← stop here
    """
    cases = []
    lines = text.split("\n")

    in_granted = False

    # Matches the exact section header "CERTIORARI GRANTED" on its own line
    granted_header = re.compile(r"^\s*CERTIORARI\s+GRANTED\s*$", re.IGNORECASE)

    # Any of these headers ends the granted section
    end_section = re.compile(
        r"^\s*(CERTIORARI\s+DENIED|CERTIORARI\s+\u2014|HABEAS CORPUS|"
        r"MANDAMUS|REHEARINGS|PROBABLE JURISDICTION|AFFIRMED|REVERSED|"
        r"DISMISSED|JUDGMENT|ORDERS IN PENDING|APPENDIX|ATTORNEY DISCIPLINE|"
        r"APPEAL\s+--|CERTIORARI\s+--)",
        re.IGNORECASE
    )

    # Docket number at start of line: "11-1234" or "11-9307", optionally with ")"
    docket_re = re.compile(r"^\s*(\d{1,2}-\d{1,5})\s*[)]*\s+(.*)")

    full_date = normalize_date(date_str, term_year)
    full_term = f"October {2000 + term_year}"

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if granted_header.match(stripped):
            in_granted = True
            i += 1
            continue

        if in_granted and end_section.match(stripped):
            in_granted = False
            i += 1
            continue

        if in_granted:
            m = docket_re.match(lines[i])
            if m:
                docket = m.group(1).strip()
                case_name_raw = m.group(2).strip()

                # Consume continuation lines that are part of the case name
                # (e.g. consolidated cases that wrap to next line)
                j = i + 1
                while j < len(lines):
                    ns = lines[j].strip()
                    if not ns:
                        break
                    if docket_re.match(lines[j]):
                        break
                    if granted_header.match(ns) or end_section.match(ns):
                        break
                    # Prose lines signal the case name is done
                    if re.match(
                        r"^(The |It |A |An |This |Petition|Motion|Justice|"
                        r"Per |Solicitor|See |Whether|Because|Under )",
                        ns
                    ):
                        break
                    case_name_raw += " " + ns
                    j += 1

                case_name = clean_case_name(case_name_raw)
                if case_name:
                    cases.append({
                        "docket": docket,
                        "case_name": case_name,
                        "date": full_date,
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
                i = j
                continue

        i += 1

    return cases


def clean_case_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    # Remove trailing page numbers
    name = re.sub(r"\s+\d+\s*$", "", name).strip()
    # Remove closing parens left from consolidated cases
    name = re.sub(r"\s*\)\s*$", "", name).strip()
    # Must contain "v." to be a real case name
    if not re.search(r"\bv\.?\s", name, re.IGNORECASE):
        return ""
    return name[:200]


def normalize_date(date_str: str, term_year: int) -> str:
    if not date_str:
        return ""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", date_str)
    if m:
        mo, day, yr = m.groups()
        if len(yr) == 2:
            yr = "20" + yr
        return f"{yr}-{int(mo):02d}-{int(day):02d}"
    return date_str


# ─── Oyez enrichment (one API call per term, no per-case fetches) ─────────────

def enrich_with_oyez(cases: list[dict]) -> list[dict]:
    """Match cases to Oyez by docket using one term-level API call per year."""
    by_term: dict[int, list] = {}
    for c in cases:
        yr_m = re.search(r"(\d{4})", c.get("term", ""))
        if yr_m:
            by_term.setdefault(int(yr_m.group(1)), []).append(c)

    for year, term_cases in sorted(by_term.items()):
        log.info(f"Fetching Oyez data for {year}...")
        oyez_list = fetch_oyez_term(year)

        # Build docket lookup with a few key variants
        lookup: dict[str, dict] = {}
        for oc in oyez_list:
            d = (oc.get("docket_number") or "").strip()
            if d:
                lookup[d] = oc
                # Strip leading zeros after dash: "14-0000" -> "14-0"
                lookup[re.sub(r"-0+(\d)", r"-\1", d)] = oc

        for c in term_cases:
            raw_docket = c["docket"]
            oc = (
                lookup.get(raw_docket)
                or lookup.get(re.sub(r"-0+(\d)", r"-\1", raw_docket))
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

            # Use Oyez decision date if we didn't get one from the PDF
            if not c["date"]:
                dd = oc.get("decision_date")
                if dd:
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


# ─── Build Excel ──────────────────────────────────────────────────────────────

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

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
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
            cell.alignment = (
                left_align if col_idx in (1, 6, 7, 8, 13) else center_align
            )

    col_widths = [45, 12, 14, 18, 16, 28, 22, 24, 14, 20, 18, 22, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "SCOTUS Certiorari Dataset — Summary"
    ws2["A1"].font = Font(bold=True, size=14, name="Calibri")
    terms = sorted(set(c.get("term", "") for c in cases))
    from datetime import datetime
    rows = [
        ("Total cases (cert granted):", len(cases)),
        ("Terms covered:", ", ".join(terms)),
        ("Generated:", datetime.now().strftime("%Y-%m-%d %H:%M UTC")),
        ("Note:", "Circuit Split / Federalism / Precedent / National Significance columns require manual review."),
    ]
    for i, (label, val) in enumerate(rows, 3):
        ws2.cell(row=i, column=1, value=label).font = Font(name="Calibri", size=10, bold=True)
        ws2.cell(row=i, column=2, value=val).font = Font(name="Calibri", size=10)
    ws2.column_dimensions["A"].width = 32
    ws2.column_dimensions["B"].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ─── Background job store ─────────────────────────────────────────────────────

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
                 msg=f"Fetching order lists for {full_year} ({idx+1}/{len(TERM_YEARS)})...",
                 pct=pct)

            order_urls = get_order_list_urls(term_year)
            if not order_urls:
                _upd(job_id, msg=f"No order lists found for {full_year}, skipping.", pct=pct)
                continue

            _upd(job_id,
                 msg=f"{full_year}: found {len(order_urls)} order lists, parsing PDFs...",
                 pct=pct)

            term_cases = []
            for pdf_info in order_urls:
                parsed = parse_order_list_pdf(pdf_info["url"], pdf_info["date"], term_year)
                term_cases.extend(parsed)
                time.sleep(0.05)

            # Deduplicate by docket number within each term
            seen: set[str] = set()
            unique = []
            for c in term_cases:
                if c["docket"] not in seen:
                    seen.add(c["docket"])
                    unique.append(c)

            all_cases.extend(unique)
            _upd(job_id,
                 msg=f"{full_year}: {len(unique)} granted-cert cases found.",
                 pct=pct)

        _upd(job_id, msg=f"Enriching {len(all_cases)} cases with Oyez data...", pct=72)
        all_cases = enrich_with_oyez(all_cases)

        _upd(job_id, msg="Building Excel file...", pct=95)
        excel_bytes = build_excel(all_cases)

        _upd(job_id,
             status="done",
             msg=f"Done — {len(all_cases):,} granted cases across {len(TERM_YEARS)} terms.",
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
