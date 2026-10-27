# LinkedIn lead kit

Finds leads for your business on LinkedIn and sorts them into your Google Sheet. Claude does the setup with you,
runs the searches and reads every lead before it reaches the sheet.

It does two kinds of search:
- **Posts asking for help**: people posting "looking for a bookkeeper", "can anyone recommend a CRM consultant".
  Warm leads with a need right now.
- **People search**: people by job title, industry, location and company size. You choose exactly who.

Each run:
1. **Search** LinkedIn through Apify scrapers. Nothing logs in to LinkedIn, so your account is never at risk.
2. **Filter**: your own rules throw out clear noise (job seekers, people selling the same thing).
3. **Judge**: Claude reads every remaining lead against your description of a good lead and puts it in a tab.
4. **Enrich** (optional): company, location, website, industry and company size from LinkedIn, plus your country rule.
5. **Sheet**: one tab per lead type, newest first. Your edits, moves, added rows and added columns are kept.

It never contacts anyone.

## Install
You need Claude (the desktop app with Claude Code, or the Claude Code CLI) and Python 3.10 or newer.

1. Download this folder (Code, then Download ZIP, then unzip), or `git clone` it.
2. Open the folder in Claude and say: **"Set up this lead kit for my business."**
3. Claude follows [SETUP.md](SETUP.md): it asks about your business, your ideal buyer, which searches you want, your
   rules and budget, tests the keywords with you, and helps you connect Apify (free account) and your Google Sheet.

After that, say **"Run the lead search"** in Claude in this folder, or let Claude set up a daily run.

## Costs
Paid to Apify, from your own account (the free plan includes $5 of credit a month):
- Post search: about $2 per 1,000 posts read.
- People search: about $0.10 per 25 people. Needs a paid Apify plan: free accounts get only 10 runs of it in total.
- Enrichment: about $0.008 per lead.
You set a monthly and a per-run limit. A search that does not fit the budget waits for the next run instead of being
cut short. Prices are read live from Apify before each run.

## Working in the sheet
- To remove a lead, set its **Status** to `drop` (or delete the row).
- Move a row to another tab and it stays there. Edit any cell and your edit sticks.
- Add your own columns after **Key**; they are kept with their formulas.
- Mark leads with the Status column rather than colours: rows re-sort on every update.
- Don't edit the sheet while an update is running.

## For developers
`python lead.py help` lists the commands; [OPERATOR.md](OPERATOR.md) is the run routine Claude follows.
`config/` (your settings and keys) and `data/` (your leads) are never committed. Tests: `python -m unittest discover tests`
(offline, spend nothing).

Scrapers used: `harvestapi/linkedin-post-search`, `harvestapi/linkedin-profile-search`,
`harvestapi/linkedin-profile-scraper`, `harvestapi/linkedin-company` on Apify.
