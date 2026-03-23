# SCOTUS Cert Scraper

Scrapes certiorari-granted cases from supremecourt.gov order lists (2011–present), enriches them with Oyez API data, and exports to Excel.

## What it collects

| Column | Source |
|---|---|
| Case Name | SCOTUS order list PDFs |
| Docket Number | SCOTUS order list PDFs |
| Date of Order | SCOTUS order list PDFs |
| Term | Derived from year |
| Granted Cert (0/1) | Always 1 (only granted cases) |
| Outcome / Winning Party | Oyez API |
| Decision Direction | Oyez API |
| Issue Area | Oyez API |
| Circuit Split | Manual (fill in Excel) |
| Federalism Conflict | Manual (fill in Excel) |
| Precedent Matter | Manual (fill in Excel) |
| National Significance | Manual (fill in Excel) |

## Deploy to Railway

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "initial commit"
gh repo create scotus-cert-scraper --public --push
```

### 2. Deploy on Railway
1. Go to https://railway.app → **New Project** → **Deploy from GitHub Repo**
2. Select this repo
3. Railway auto-detects the Dockerfile
4. Click **Deploy** — done

### Notes
- Scraping all terms takes **3–7 minutes** — Railway's default timeout is fine
- The scraper hits supremecourt.gov and api.oyez.org — both are public
- Gunicorn timeout is set to 600s to handle long scrape runs
