# Setup: for the Claude installing this kit

You are setting this kit up for one business, on their computer. By the end, `python lead.py check` prints READY, the
first run has put real leads in their Google Sheet, and they know how runs happen from now on.

How to work:
- **Ask before you decide.** Every setting below is the client's call. Ask one topic at a time, in plain words, and
  keep asking until you both agree. Never fill a gap with a guess. Write their answers into the config files as you go.
- **Plain language.** Most clients are not technical. Explain each step as what they will see, not how it works.
- **Secrets stay out of chat.** For the Apify token and the Google key, have the client put the file in place
  themselves (open the folder or Notepad for them). Never print a token.
- **Spend only what they approve.** Say the cost before any paid step. Keyword tests cost cents; say how many.

Files you will write (both are ignored by git):
- `config/profile.json`: who they are and their rules. Start from `examples/profile.example.json`.
- `config/searches.json`: their searches. Start from `examples/searches.example.json`.

## 0. Install
1. Python 3.10 or newer. Then: `pip install -r requirements.txt`
2. `python -m unittest discover tests` must pass (offline, spends nothing).

## 1. The business
Ask, and write into `profile.json` "business" and "timezone":
- Name, and what they sell (each service in a few words).
- Who a good lead is: who buys, what they would be posting or what their job is, company size, places.
- Who is not a lead even if they look like one (competitors, job seekers, students, sellers of the same service).
- Their time zone (IANA name, e.g. `Europe/London`, `America/New_York`, `Asia/Kolkata`).

## 2. Which LinkedIn search
Explain both, then ask which they want (one, or both):
- **Posts asking for help**: finds LinkedIn posts where someone asks for what the client sells ("looking for a
  bookkeeper", "can anyone recommend a CRM consultant"). These are warm: the person has a need right now. About $2 per
  1,000 posts read. Works best for services people openly ask for.
- **People search**: finds people by job title, industry, location, company size and so on ("finance directors at
  50-200 person UK manufacturers"). Cold, but you choose exactly who. About $0.10 per 25 people. **Needs a paid Apify
  plan**: on the free plan this scraper allows only 10 runs in total, then returns nobody. Tell them before they choose.

## 3. The tabs
Each lead type gets its own tab in the sheet. Ask what kinds of lead they want kept apart, and one line saying what
each means. Two or three is usual, e.g. "Hot: a business asking for our service for itself", "Partner: an agency
looking for a subcontractor". `Review` (unsure) and `Excluded` (real leads their rules rule out) are always added.
Write them to "tabs". The judge reads these lines on every page, so make them exact.

## 4. The searches
Draft searches from their services, show them in plain words, and agree on them before testing.

**Post searches** (`"type": "posts"`): a LinkedIn boolean query in "q".
- At most 5 AND/OR/NOT operators (outside quotes), straight quotes only, 500 characters. `python lead.py check` tests this.
- Write the ask, not the topic: `"looking for" AND (bookkeeper OR "bookkeeping service")` finds buyers;
  `bookkeeping` finds bookkeepers advertising. Mix ask phrases ("looking for", "recommend", "need a", "anyone know")
  with their service words, the way a buyer would write it.
- When the service is also a job title (bookkeeper, accountant, designer, developer), ask phrases mostly catch job
  ads and providers advertising. Counter it with a reject rule for in-house job ads (the example profile has one),
  and `author_keywords` so the poster must be a likely buyer.
- Optional `author_keywords` (the poster's headline must contain one, e.g. `founder OR owner OR CEO`) and
  `industry_ids` (LinkedIn industry ids of the poster).
- `max_per_run`: posts bought per run (default 50). Daily runs need fewer than weekly ones.

**People searches** (`"type": "people"`): `search_query` (optional keywords) plus `filters`.
- `python lead.py fields people` lists every filter and its allowed values (free). Copy names exactly, e.g.
  `{"currentJobTitles": ["Finance Director", "CFO"], "locations": ["United Kingdom"], "companyHeadcount": [...]}`.
- `pages_per_run`: pages of 25 people per run (default 1, $0.10 a page). Each run takes the next pages; a search that
  runs out of pages is marked finished.

**Test every search before it goes live** (tell them the cost first, about $0.03 per post test, $0.10 per people test):
```
python lead.py search --test P01,P02 --max 15
```
It prints one line per result. Count how many look like real leads, show the client the count and a few examples,
and agree which searches to keep (`"active": true`), rewrite or drop. Two or three rounds is normal. Results from
tests also go to the judge queue, so nothing paid for is wasted.

## 5. The rules
Ask about each, and write their answers into "rules" and "judge_notes":
- **Clear noise to throw out before judging** ("reject"): e.g. job seekers ("open to work", "looking for a new role"),
  people selling the same service ("we offer", "DM me for"). Patterns are case-insensitive regular expressions with
  a reason: `{"pattern": "\\bopen to work\\b", "why": "job seeker"}`. Keep this list short: anything uncertain should
  reach the judge. Optional "must_match": a post must contain one of these to be kept.
- **Send to Review** ("review"): things a person should look at.
- **Company pages** ("company_pages"): posts written by a company page: keep, review (default) or reject.
- **Countries**: do they serve only some countries ("only_countries"), or want to leave some out
  ("exclude_countries")? Each country as `{"name": "United Kingdom", "code": "GB", "aliases": ["UK", "England",
  "London"]}`: the two-letter ISO code, plus aliases (cities, regions or other names that mean that country, as
  LinkedIn writes locations). Then "country_basis": does the **company's** location decide (default), the
  **poster's**, or **either**? And "exclude_unknown": drop leads whose location nobody can find (default no)?
  A lead their rule rules out goes to the Excluded tab with the evidence, never deleted, so they can check it.
- **Anything else the judge should know** ("judge_notes"), in one paragraph: e.g. "a post that needs the provider
  on-site outside the UK goes to Excluded", "students and recruiters are never leads".

## 6. Enrichment
Ask whether they want each kept lead filled with company, location, website, industry and company size from LinkedIn
(about $0.008 a lead). Required for an exact country rule by company. Which tabs to fill (default: every lead tab and
Review). Write "enrich": {"enabled": true|false, "tabs": [...]}.

## 7. Budget
Explain: Apify's free plan gives $5 of credit a month; paid plans give more. Ask:
- "monthly_usd": the most to spend in one Apify billing cycle (leave out to use whatever the account has).
- "per_run_usd": the most one run may spend.
A search that does not fit what is left waits whole for the next run; nothing is cut short or lost.
Give a rough monthly cost from their searches: posts `max_per_run x $0.002`, people `pages_per_run x $0.10`, enrichment
`$0.008 x leads`, times runs a month.

## 8. Accounts and keys
**Apify** (the scraper; no LinkedIn login is ever used):
1. They sign up at apify.com (free).
2. Settings, then API & Integrations: copy the Personal API token.
3. They paste it into `config/apify_token.txt` themselves (one line, nothing else). Open the file for them.

**Google Sheet**:
1. They create an empty Google Sheet and send you its URL; the id is the long part between `/d/` and `/edit`.
   Write it to "sheet_id".
2. A service account (a robot Google user that writes the sheet). Walk them through, one click at a time:
   console.cloud.google.com, create a project, enable "Google Sheets API", then IAM & Admin, Service Accounts,
   Create (no roles needed), then Keys, Add key, JSON. The file downloads. Some company Google accounts block key
   downloads; then make the project under a personal Google account instead (this works when their company lets
   sheets be shared outside it).
3. They move that file to `config/google_key.json`.
4. They share the sheet with the service account's email (in the file as client_email; `python lead.py check` prints it)
   as Editor.

## 9. Check and first run
1. `python lead.py check` until it prints READY.
2. Tell them the first run's worst case (`python lead.py search --dry-run`), get their go, then follow OPERATOR.md once
   with them watching. Post searches look back up to 14 days on the first run.
3. Show them the sheet: which tab is which, Status "drop" removes a row, edits and added columns are kept.

## 10. How runs happen from now on
Ask: daily at a set time, or only when they ask?
- **Daily**: create a scheduled task in their Claude app (or a cron job that starts Claude) at their chosen time whose
  prompt is: "Open <this folder> and do one lead search run by following OPERATOR.md." Runs need Claude, because
  Claude is the judge. Twice a day suits busy post searches.
- **On request**: tell them to say "run the lead search" in a Claude session opened in this folder.
Write their choice to "schedule" in profile.json, so a later session knows.

## Done when
`check` says READY, the first run's leads are in their sheet, they agreed every setting, and they know how the next
run starts.
