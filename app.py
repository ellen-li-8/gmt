import os, re, io, time, logging
from flask import Flask, render_template, jsonify, send_file, Response
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

TERM_YEARS = list(range(11, 26))  # 2011–2025 (SCOTUS uses 2-digit year)


# ─── SCOTUS scraping ──────────────────────────────────────────────────────────

def get_order_list_urls(term_year: int) -> list[dict]:
    """Fetch all Order List PDF URLs for a given 2-digit SCOTUS term year."""
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
        text = a.get_text(strip=True)
        if "zor.pdf" in href.lower() or "order list" in text.lower():
            full_url = href if href.startswith("http") else "https://www.supremecourt.gov" + href
            date_match = re.search(r"(\d{2}/\d{2}/\d{2,4})", a.parent.get_text(" ") if a.parent else "")
            date_str = date_match.group(1) if date_match else ""
            results.append({"url": full_url, "date": date_str})
    return results


def parse_order_list_pdf(pdf_url: str, date_str: str, term_year: int) -> list[dict]:
    """Download a PDF and extract certiorari-granted cases."""
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
    Parse raw PDF text and extract cases where certiorari was granted.
    The PDFs have sections like:
      CERTIORARI GRANTED
      11-1234  SMITH V. JONES
      ...
      CERTIORARI DENIED
    """
    cases = []
    lines = text.split("\n")

    in_granted_section = False
    # These headers end a granted section
    section_enders = re.compile(
        r"CERTIORARI DENIED|HABEAS CORPUS|MANDAMUS|REHEARINGS|JUDGMENT|DISMISSED|"
        r"PROBABLE JURISDICTION|NOTED|AFFIRMED|REVERSED|CERTIORARI —",
        re.IGNORECASE
    )

    # Docket number pattern: e.g. "11-1234" or "11A56" or "No. 11-1234"
    docket_re = re.compile(r"^\s*(\d{1,2}[-A-Z]\d{3,5})\s+(.+)$")
    # Multi-case consolidated: lines with just a docket and ")"
    consolidated_re = re.compile(r"^\s*(\d{1,2}[-A-Z]\d{3,5})\s*[)]\s*(.*)$")

    # Format date string to full year
    full_date = normalize_date(date_str, term_year)
    full_term = term_year_to_full(term_year, full_date)

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if re.search(r"CERTIORARI GRANTED", line, re.IGNORECASE):
            in_granted_section = True
            i += 1
            continue

        if in_granted_section and section_enders.search(line):
            in_granted_section = False

        if in_granted_section:
            # Try to match a docket + case name
            m = docket_re.match(lines[i])
            if not m:
                m = consolidated_re.match(lines[i])
            if m:
                docket = m.group(1).strip()
                case_name_raw = m.group(2).strip()

                # Collect continuation lines (indented or short)
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if not next_line:
                        break
                    # Stop if next line is another docket or a section header
                    if docket_re.match(lines[j]) or section_enders.search(next_line):
                        break
                    # Stop if it starts to look like prose (description text)
                    if re.match(r"^(The |It |A |An |This )", next_line):
                        break
                    case_name_raw += " " + next_line
                    j += 1

                case_name = clean_case_name(case_name_raw)
                if case_name:
                    cases.append({
                        "docket": docket,
                        "case_name": case_name,
                        "date": full_date,
                        "term": full_term,
                        "granted_cert": 1,
                        "source_url": source_url,
                        # Oyez fields filled in later
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
    """Clean up extracted case name text."""
    # Remove trailing/leading noise
    name = re.sub(r"\s+", " ", raw).strip()
    # Strip docket references
    name = re.sub(r"\s*\d{1,2}[-A-Z]\d{3,5}\s*", " ", name).strip()
    # Strip parenthetical crud from consolidations
    name = re.sub(r"\s*[()]\s*$", "", name).strip()
    # Must have V. or v. to be a real case
    if not re.search(r"\bv\.?\s", name, re.IGNORECASE):
        return ""
    return name[:200]


def normalize_date(date_str: str, term_year: int) -> str:
    """Convert scraped date string to YYYY-MM-DD."""
    if not date_str:
        return ""
    # mm/dd/yy or mm/dd/yyyy
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", date_str)
    if m:
        mo, day, yr = m.groups()
        if len(yr) == 2:
            yr = "20" + yr
        return f"{yr}-{int(mo):02d}-{int(day):02d}"
    return date_str


def term_year_to_full(term_year: int, date_str: str) -> str:
    """Return human-readable term like 'October 2011'."""
    full_year = 2000 + term_year
    # SCOTUS term starts in October
    return f"October {full_year}"


# ─── Oyez enrichment ──────────────────────────────────────────────────────────

def enrich_with_oyez(cases: list[dict]) -> list[dict]:
    """Look up each case on Oyez API to add outcome and issue area."""
    # Group by term year for efficient API calls
    by_term: dict[str, list] = {}
    for c in cases:
        term = c.get("term", "")
        yr_match = re.search(r"(\d{4})", term)
        if yr_match:
            by_term.setdefault(yr_match.group(1), []).append(c)

    oyez_lookup: dict[str, dict] = {}  # docket -> oyez data

    for yr_str, term_cases in by_term.items():
        log.info(f"Fetching Oyez data for term {yr_str}...")
        oyez_cases = fetch_oyez_term(int(yr_str))
        for oc in oyez_cases:
            docket = oc.get("docket_number", "").strip()
            if docket:
                oyez_lookup[docket] = oc
        time.sleep(0.3)

    for c in cases:
        docket = c.get("docket", "")
        # Oyez uses "11-1234" format too; try direct match first
        oc = oyez_lookup.get(docket)
        if not oc:
            # Try without leading zeros
            oc = oyez_lookup.get(re.sub(r"^0+", "", docket))
        if oc:
            # Decision
            decision = oc.get("decisions") or []
            if decision:
                d = decision[0]
                c["decision_direction"] = d.get("decision_direction", "") or ""
                winning_party = d.get("winning_party", "") or ""
                c["outcome"] = winning_party
            c["issue_area"] = oc.get("first_party_label", "") or ""
            c["oyez_url"] = oc.get("href", "").replace("api.oyez.org", "www.oyez.org") if oc.get("href") else ""

    return cases


def fetch_oyez_term(year: int) -> list[dict]:
    """Fetch all cases for a SCOTUS term year from Oyez API."""
    url = f"https://api.oyez.org/cases?filter=term:{year}&per_page=0"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Oyez fetch failed for {year}: {e}")
        return []


# ─── Excel export ─────────────────────────────────────────────────────────────

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
        "Oyez URL", "Order List PDF",
    ]

    # Header row style
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

    # Data rows
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
            c.get("source_url", ""),
        ]
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = row_font
            cell.border = border
            if fill:
                cell.fill = fill
            if col_idx in (1, 6, 7, 8, 13, 14):
                cell.alignment = left_align
            else:
                cell.alignment = center_align

    # Column widths
    col_widths = [45, 12, 14, 18, 16, 28, 22, 24, 14, 20, 18, 22, 40, 40]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = "SCOTUS Certiorari Dataset — Summary"
    ws2["A1"].font = Font(bold=True, size=14, name="Calibri")
    ws2["A3"] = "Total cases (granted cert):"
    ws2["B3"] = len(cases)
    terms = sorted(set(c.get("term", "") for c in cases))
    ws2["A4"] = "Terms covered:"
    ws2["B4"] = ", ".join(terms)
    ws2["A5"] = "Generated:"
    from datetime import datetime
    ws2["B5"] = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    for row in ws2.iter_rows(min_row=3, max_row=5, min_col=1, max_col=2):
        for cell in row:
            cell.font = Font(name="Calibri", size=10)
    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 60

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape")
def scrape():
    """SSE endpoint: streams progress + final result as JSON."""
    def generate():
        all_cases = []
        total_terms = len(TERM_YEARS)

        for idx, term_year in enumerate(TERM_YEARS):
            full_year = 2000 + term_year
            yield f"data: {{'type':'progress','msg':'Fetching order lists for {full_year} term ({idx+1}/{total_terms})...','pct':{int((idx/total_terms)*50)}}}\n\n"

            order_urls = get_order_list_urls(term_year)
            if not order_urls:
                yield f"data: {{'type':'progress','msg':'No order lists found for {full_year}, skipping.','pct':{int((idx/total_terms)*50)}}}\n\n"
                continue

            yield f"data: {{'type':'progress','msg':'Found {len(order_urls)} order lists for {full_year}. Parsing PDFs...','pct':{int((idx/total_terms)*50)}}}\n\n"

            term_cases = []
            for pdf_info in order_urls:
                parsed = parse_order_list_pdf(pdf_info["url"], pdf_info["date"], term_year)
                term_cases.extend(parsed)
                time.sleep(0.1)

            # Deduplicate within term by docket number
            seen = set()
            unique = []
            for c in term_cases:
                key = c["docket"]
                if key not in seen:
                    seen.add(key)
                    unique.append(c)

            all_cases.extend(unique)
            yield f"data: {{'type':'progress','msg':'Found {len(unique)} granted-cert cases for {full_year}.','pct':{int((idx/total_terms)*50)}}}\n\n"
            time.sleep(0.2)

        yield f"data: {{'type':'progress','msg':'Enriching {len(all_cases)} cases with Oyez data...','pct':55}}\n\n"
        all_cases = enrich_with_oyez(all_cases)

        yield f"data: {{'type':'progress','msg':'Building Excel file...','pct':90}}\n\n"

        import json, base64
        excel_bytes = build_excel(all_cases)
        b64 = base64.b64encode(excel_bytes).decode()

        # Emit the cases as JSON for preview
        preview = all_cases[:200]  # first 200 for UI display
        payload = json.dumps({
            "type": "done",
            "total": len(all_cases),
            "preview": preview,
            "excel_b64": b64,
        })
        yield f"data: {payload}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.route("/api/download", methods=["POST"])
def download():
    from flask import request as freq
    import json, base64
    data = freq.get_json()
    cases = data.get("cases", [])
    excel_bytes = build_excel(cases)
    return send_file(
        io.BytesIO(excel_bytes),
        download_name="scotus_cert_cases.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
