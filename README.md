# SCOTUS Cert Tracker

A collaborative web app for tracking SCOTUS certiorari decisions.

## Features
- Add cases with name, date, term, and cert status (Granted/Denied)
- Tag each case with cert buckets: Circuit Split, Federalism Conflict, Precedent Matter, National Significance
- Live stats: total cases, granted count, grant rate
- Export to `.xlsx` (formatted for R analysis)

## Deploy to Railway

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "initial commit"
gh repo create scotus-cert-tracker --public --push
```

### 2. Deploy on Railway
1. Go to https://railway.app → New Project → Deploy from GitHub Repo
2. Select this repo
3. Railway auto-detects the Dockerfile — click **Deploy**
4. Done. Share the URL with your team.

> Data is stored in each user's browser (localStorage). Export to .xlsx to consolidate.
