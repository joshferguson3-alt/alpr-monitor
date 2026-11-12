# ALPR Signal Monitor: build spec

Nov 11, 2026 · @Josh

## How to use this spec

This document is a complete build brief for a coding model. Build exactly what is described, in the order given under Build order. Where the spec says "verify", check the live documentation or page before writing code, because API details change. Ask the user only for the items listed under Inputs needed.

## Purpose

A salesperson covering law enforcement agencies in the Southeast sells fixed automated license plate reader (ALPR) cameras against an incumbent vendor, Flock Safety. Buying decisions run through public bodies, so the signals are public. This tool watches those public sources every day, extracts anything about ALPR, classifies it, scores it, and delivers a short ranked digest.

The question it answers each morning: which agencies moved closer to or further from an ALPR decision yesterday, and who said what about it.

## Scope

**Version 1 (build this):**

- 6 agencies in metro Atlanta, listed in a config file.
- State legislatures of Georgia and Tennessee.
- An influencer watchlist of public officials and organizations (categories below).
- Daily run, results in a Notion database, plus a daily email digest.

**Designed for, not built yet:** about 200 agencies across Georgia, Tennessee, Alabama, the Florida Panhandle, and the Carolinas. Adding an agency must require only a new config entry, never new code, unless its meeting platform has no collector yet.

**Out of scope:** any non-public data, any login-gated source, any paid data vendor, social media platforms that require a paid API, and outreach automation of any kind.

## Success criteria

1. Adding an agency is a config change of under 10 lines.
2. A daily run finishes in under 15 minutes and costs under $0.50 in model usage.
3. At least 80% of items in the digest are truly about ALPR or surveillance cameras, measured on a hand-checked sample of 50.
4. Every item links to its original public source and quotes the passage it was classified from.
5. A single failing source never stops the run.

## Architecture

A single Python program that runs once a day on a schedule. Each run moves documents through ten stages in order.

1. **Load registry.** Read the YAML config of agencies, people, organizations, and sources.
2. **Collect.** Run one collector per source. Each collector returns a list of raw documents: `source_id`, `url`, `title`, `published_at`, `doc_type`, `text`.
3. **Deduplicate.** Skip any document whose URL or SHA-256 content hash is already in the database.
4. **Keyword prefilter.** Keep only documents containing at least one term from the keyword list. This step uses no model and removes most documents.
5. **Extract passages.** For each keyword hit, cut a window of about 1,500 characters on each side. Merge overlapping windows. Long agendas and transcripts are never sent whole.
6. **Classify.** Send each passage to a small language model with the prompt in this spec. It returns one JSON object per passage.
7. **Resolve entities.** Match the agency, speaker, and organization names in the JSON to registry IDs. Unmatched names are kept as text and flagged for review.
8. **Score.** Apply the scoring rules to produce a 0 to 100 priority.
9. **Store and publish.** Write the signal to SQLite, then create or update its row in the Notion database.
10. **Digest and log.** Email the day's signals ranked by score, and write a run log with per-source counts and errors.

### Tech stack

| Piece | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.11 or later | Best library support for scraping and PDFs |
| Scheduler | GitHub Actions cron, once daily at 6:00 AM Eastern | Free, no server to maintain |
| State | One SQLite file committed back to the repo after each run | Simple, versioned, inspectable |
| HTTP | `httpx` with a 20-second timeout and 2 retries |  |
| HTML parsing | `selectolax` or `beautifulsoup4` |  |
| PDF text | `pypdf` first; fall back to `pdfplumber` | Agenda packets are PDFs |
| Feeds | `feedparser` | RSS and Atom |
| Video captions | `youtube-transcript-api` | Council meeting recordings |
| Classifier | A small, low-cost model through the Anthropic API. Verify the current model ID in the Anthropic documentation. | Structured extraction at low cost |
| Output | Notion API (official `notion-client`) | The user already works in Notion |
| Email | SMTP through Gmail with an app password | No extra service |
| Config | YAML files in `config/` | Editable without touching code |

### Repository layout

```
alpr-monitor/
  config/
    agencies.yaml
    people.yaml
    organizations.yaml
    sources.yaml
    keywords.yaml
  monitor/
    run.py              # entry point; orchestrates the ten stages
    registry.py         # loads and validates config
    collectors/
      base.py           # Collector interface and RawDocument dataclass
      legistar.py
      civicplus.py
      generic_html.py
      rss.py
      legiscan.py
      openstates.py
      youtube.py
      flock_transparency.py
    prefilter.py
    passages.py
    classify.py
    resolve.py
    score.py
    store.py            # SQLite
    notion_sync.py
    digest.py
  tests/
    fixtures/           # saved sample pages and PDFs
  .github/workflows/daily.yml
  README.md
```

### Rules for every collector

- One class per platform, never one per agency. Agency differences live in config.
- Fetch only items published in the last 14 days on a normal run, and the last 90 days on first run for a source.
- Wrap every source in its own try/except. Log the error and continue.
- Identify the client with a descriptive User-Agent, obey `robots.txt`, and wait at least 2 seconds between requests to the same host.

## Who and what to watch

The registry has three entity types. Every signal must link to at least one of them.

### Agencies (`agencies.yaml`)

```yaml
- id: brookhaven-pd
  name: Brookhaven Police Department
  state: GA
  type: municipal_police        # or county_police, sheriff
  sworn_officers: null          # fill in; verify from a public source
  segment: null                 # inside (1-40), mid_city (41-100), t1200 (101-550), major (551+)
  governing_body: Brookhaven City Council
  meeting_platform: null        # legistar, civicplus, municode, novusagenda, boarddocs, primegov, escribe, other
  meeting_url: null
  youtube_channel: null
  alpr_vendor: null             # flock, axon, motorola, other, none, unknown
  alpr_camera_count: null
  flock_transparency_url: null
  contract_end_date: null
  legislative_districts: {state_house: [], state_senate: [], us_house: []}
```

Starter candidates for version 1, all to be verified before use: Brookhaven PD, Dunwoody PD, Sandy Springs PD, Marietta PD, Cobb County PD, Gwinnett County PD. For each, confirm the sworn officer count, the meeting platform, and the current ALPR vendor. Replace any that fall outside the 41 to 550 sworn range.

### Watchlist categories (`people.yaml` and `organizations.yaml`)

Each person entry holds: `id`, `name`, `role`, `body`, `state`, `district`, linked `agency_ids`, and `source_urls` (official page, press release feed).

| Category | Who | Why it matters |
| --- | --- | --- |
| Agency leadership | Chief or sheriff, deputy chiefs, real-time crime center lead, IT director, procurement officer | They request the purchase |
| Local government | Mayor, council members or county commissioners, public safety committee members, city or county manager, city attorney | They approve it |
| State legislators | Georgia and Tennessee members who sponsor ALPR, surveillance, or data privacy bills; chairs and members of public safety, judiciary, and technology committees; members whose districts cover a registry agency | They set retention, sharing, and use rules |
| Congressional delegation | U.S. senators and representatives for the covered states and districts, plus any member of Congress who has taken a public position on ALPR | Federal scrutiny shapes local debate |
| State executive | Governor, attorney general (formal opinions), state bureau of investigation, state procurement office, state department of transportation | Opinions, statewide contracts, and right-of-way permits for camera poles |
| Law enforcement associations | State chiefs' and sheriffs' associations, police unions | They set the profession's position |
| Local government associations | State municipal league and county commissioners' association | Model policies and legislative positions |
| Civil liberties and advocacy | ACLU national and state affiliates, EFF, Institute for Justice, local privacy groups, crowdsourced camera-mapping projects | Organized opposition and litigation |
| Funders | Community improvement districts, business districts, homeowner associations, local foundations, grant programs, sales-tax capital programs | They often pay for cameras outside the police budget |
| Courts and oversight | Courts hearing ALPR cases, city auditors, inspectors general, district attorneys | Rulings and audits change what is allowed |
| Vendors | Flock, Axon, Motorola, and other ALPR vendors: press releases, policy changes, transparency pages | Competitor moves and product changes |
| Media | Local newspapers and TV, public safety trade press, outlets that investigate ALPR | Coverage drives council pressure |
| Public commenters | Residents speaking at meetings | Volume and direction of community sentiment |

### Privacy rule for people

Track named individuals only when they are public officials or organization spokespeople speaking in an official capacity. For residents who speak during public comment, store counts and stance only (for example, "7 speakers opposed, 2 in favor"), never names. Do not collect home addresses, personal social accounts, or anything unrelated to ALPR.

### Populating the registry

1. Agency leadership and local officials: from each government's official website.
2. State legislators, committees, and districts: from the legislative data API (see Sources).
3. Organizations: enter by hand from the category table; about 25 entries cover both states.
4. Write a one-time script, `scripts/seed_registry.py`, for steps 1 and 2. It must output YAML for the user to review, never write to the live config directly.

## Sources and collectors

Every endpoint, URL pattern, and rate limit below must be verified against live documentation before coding. Use free tiers only.

### Local government

| Source | Yields | How to collect | Cadence |
| --- | --- | --- | --- |
| Council and commission agendas, minutes, agenda packets | Purchase approvals, contract renewals, policy votes, work-session discussion | One collector per meeting platform. Legistar has a public web API. CivicPlus, Municode, NovusAgenda, BoardDocs, PrimeGov, and eScribe need HTML listing scrapers plus PDF download. Provide a `generic_html` collector driven by CSS selectors in config as the fallback. | Daily |
| Meeting video | What officials and residents said, which minutes omit | YouTube channel feed for new videos, then captions. Process only videos whose matching agenda passed the keyword prefilter. | Daily |
| Budget documents and capital plans | Line items for cameras, renewal amounts, funding source | PDF download from the finance page; keyword search by page | Monthly |
| Procurement postings | Requests for proposals, sole-source notices, cooperative purchases | Agency bid page via `generic_html` | Weekly |
| Flock transparency portal, where an agency publishes one | Camera count, retention period, sharing partners, search volume | Fetch the page, store a snapshot, and diff against the previous one. A change is a signal. | Weekly |
| Agency and city press releases | Announcements, crime-reduction claims, pilot programs | RSS where offered, otherwise `generic_html` | Daily |

### State and federal

| Source | Yields | How to collect | Cadence |
| --- | --- | --- | --- |
| State bills (Georgia, Tennessee) | New bills, amendments, hearings, votes on ALPR, surveillance, data retention, and privacy | LegiScan API keyword search per state, then bill detail for matches. Track every matched bill until the session ends. | Daily. Note that pre-filing starts before each January session. |
| Legislators, committees, districts | Who sponsors, who sits on which committee, who represents each agency | Open States API | Monthly |
| Committee hearing agendas and video | Testimony and member statements | Legislature website; `generic_html` plus captions where available | Daily in session |
| Legislator and delegation statements | Public positions | Official press pages and RSS; a news search per person combined with ALPR terms | Daily |
| Federal bills and letters | Congressional action on ALPR and data brokers | Congress.gov API keyword search; member press feeds | Weekly |
| Attorney general opinions | Formal legal interpretations | State attorney general opinion pages via `generic_html` | Weekly |
| Court filings and rulings | ALPR lawsuits and decisions | CourtListener API search, filtered to the covered states and federal circuits | Weekly |

### Organizations and media

| Source | Yields | How to collect | Cadence |
| --- | --- | --- | --- |
| Advocacy groups | Campaigns, reports, lawsuits, public records findings | Blog and press RSS for each organization in the registry | Daily |
| Associations | Model policies, legislative positions, conference agendas | News pages via RSS or `generic_html` | Weekly |
| Vendor announcements | Product launches, policy changes, customer wins and losses | Press and blog RSS for each ALPR vendor | Daily |
| News | Local coverage naming an agency or official | One news-search RSS query per agency and per state, combined with ALPR terms; direct RSS from the main local outlets and trade press | Daily |
| Public datasets | Which agencies use which surveillance vendor | EFF Atlas of Surveillance data download, used once to seed `alpr_vendor` and refreshed quarterly | Quarterly |

### Keyword list (`keywords.yaml`)

**Strong terms** (one match is enough): license plate reader, license plate recognition, ALPR, plate reader, Flock Safety, Flock camera, Axon Outpost, Axon Lightpost, Fusus, Vigilant, Rekor, real-time crime center, vehicle recognition camera.

**Weak terms** (need a second term or a strong term within 300 characters): Flock, LPR, RTCC, surveillance camera, hot list, data retention, camera contract, Motorola Solutions, Axon.

Handle two known false positives: "LPR" has unrelated meanings, and "flock" is a common word. Match "Flock" only when capitalized mid-sentence or next to camera, safety, contract, or license.

### Source config example (`sources.yaml`)

```yaml
- id: brookhaven-council
  collector: civicplus
  entity: brookhaven-pd
  url: null            # verify and fill in
  cadence: daily
- id: ga-bills
  collector: legiscan
  state: GA
  cadence: daily
- id: aclu-ga-news
  collector: rss
  entity: aclu-georgia
  url: null
  cadence: daily
```

## Signal types

| Type | Meaning | Examples |
| --- | --- | --- |
| `contract_event` | A purchase, renewal, expansion, extension, expiration, termination, or request for proposals for ALPR or related cameras | Consent agenda item renewing a camera contract |
| `budget_event` | A budget or capital line for cameras | Proposed budget adds or cuts camera funding |
| `policy_event` | A local policy or ordinance on retention, sharing, access, or oversight | Council adopts a 30-day retention rule |
| `legislation_event` | A state or federal bill is filed, heard, amended, voted on, or signed | Bill limiting data sharing gets a committee hearing |
| `stance_statement` | A named official or organization states a position | Commissioner says she will vote against renewal |
| `public_sentiment` | Residents comment at a meeting | Nine speakers during public comment |
| `incident` | Misuse, breach, audit finding, lawsuit, vandalism, or a notable case solved | Audit finds out-of-state searches |
| `vendor_move` | A vendor wins, loses, launches, or changes policy | Competitor announces a retention change |
| `leadership_change` | A new chief, sheriff, manager, or election result | New chief sworn in |
| `funding_event` | A grant, district, or outside funder pays for cameras | Improvement district funds 20 cameras |

## Classification

Send one passage per request. Require JSON only. Reject and retry once if the response does not parse.

### System prompt

```
You extract structured facts about automated license plate readers (ALPR) and
police surveillance cameras from public government and news text.
Return one JSON object and nothing else. Use only what the passage states.
If a field is not stated, use null. Never guess names, dates, or amounts.
If the passage is not about ALPR or police surveillance cameras, return
{"is_relevant": false}.
```

### User message template

```
Source type: {doc_type}
Source title: {title}
Published: {published_at}
Known agency for this source (may be null): {agency_name}

Passage:
"""
{passage}
"""

Return JSON with these fields:
is_relevant        boolean
signal_type        one of: contract_event, budget_event, policy_event,
                   legislation_event, stance_statement, public_sentiment,
                   incident, vendor_move, leadership_change, funding_event
summary            one sentence, at most 40 words
agency_name        the law enforcement agency concerned, or null
vendors            list of vendor names mentioned
speaker_name       the official or spokesperson speaking, or null.
                   For private residents use null.
speaker_role       title and body, or "resident" for public comment
organization       organization the speaker represents, or null
stance             one of: supports, opposes, conditional, neutral, unclear
stance_target      one of: alpr_generally, incumbent_vendor, specific_vendor,
                   specific_policy
resident_counts    {"support": n, "oppose": n} for public comment, else null
key_date           ISO date of the vote, deadline, hearing, or contract end
key_date_meaning   short label for key_date
dollar_amount      number or null
camera_count       number or null
bill_id            bill number if any
quote              the single most informative sentence, copied exactly,
                   at most 300 characters
confidence         0.0 to 1.0
```

## Scoring

Start from the base score for the signal type, add modifiers, multiply by `confidence`, and cap at 100.

| Type | Base | Modifiers |
| --- | --- | --- |
| `contract_event` | 60 | +20 if the agency's current vendor is the incumbent; +15 if `key_date` is within 180 days; +10 if termination or non-renewal is discussed |
| `leadership_change` | 50 | +10 if at a registry agency |
| `incident` | 50 | +20 if at a registry agency; +10 if it names the incumbent |
| `policy_event` | 50 | +15 if at a registry agency |
| `budget_event` | 45 | +15 if `dollar_amount` is present |
| `funding_event` | 45 | +10 if at a registry agency |
| `legislation_event` | 40 | +20 if a hearing is scheduled; +35 if it passed a chamber; +50 if signed |
| `vendor_move` | 35 | +20 if in a covered state |
| `stance_statement` | 30 | +20 if the speaker sits on the governing body of a registry agency; +15 if the stance differs from that speaker's last recorded stance |
| `public_sentiment` | 25 | +15 if five or more speakers; +10 if at a registry agency |

**Tiers:** 70 and above is Act now, 40 to 69 is Watch, below 40 is Background. Signals under 20 are stored but left out of the digest.

### Stance tracking

Keep a stance history per person and per organization. When a new stance differs from the last one, create an additional `stance_statement` signal with the summary prefixed "Stance shift:".

### Account temperature

For each agency, compute a rolling 90-day sum of signal scores, with opposition to the incumbent and contract events counted at full weight and all else at half. Show it as a column so agencies can be ranked.

## Data model (SQLite)

```sql
CREATE TABLE documents (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  title TEXT,
  doc_type TEXT,            -- agenda, minutes, packet, transcript, bill, news, press, court, portal
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  passed_prefilter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE signals (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  signal_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  agency_id TEXT,           -- registry id, or null
  agency_name_raw TEXT,
  person_id TEXT,
  speaker_name_raw TEXT,
  speaker_role TEXT,
  organization_id TEXT,
  vendors TEXT,             -- JSON list
  stance TEXT,
  stance_target TEXT,
  resident_support INTEGER,
  resident_oppose INTEGER,
  key_date TEXT,
  key_date_meaning TEXT,
  dollar_amount REAL,
  camera_count INTEGER,
  bill_id TEXT,
  quote TEXT,
  confidence REAL,
  score INTEGER,
  tier TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE stances (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL,   -- person or organization
  subject_id TEXT NOT NULL,
  stance TEXT NOT NULL,
  signal_id INTEGER NOT NULL REFERENCES signals(id),
  recorded_at TEXT NOT NULL
);

CREATE TABLE snapshots (        -- for pages that are diffed, such as transparency portals
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  data TEXT NOT NULL            -- JSON of the extracted fields
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  docs_fetched INTEGER, docs_new INTEGER, passages_classified INTEGER,
  signals_created INTEGER, errors TEXT   -- JSON list of {source_id, message}
);
```

## Outputs

### Notion database: Signals

Create it once with a setup script, then upsert by `signal id`.

| Property | Type |
| --- | --- |
| Summary | Title |
| Tier | Select: Act now, Watch, Background |
| Score | Number |
| Type | Select (the ten signal types) |
| Agency | Select |
| State | Select |
| Speaker | Text |
| Role | Text |
| Stance | Select |
| Vendors | Multi-select |
| Key date | Date |
| Key date meaning | Text |
| Amount | Number, dollars |
| Cameras | Number |
| Bill | Text |
| Quote | Text |
| Source | URL |
| Published | Date |
| Needs review | Checkbox |
| Signal ID | Number |

Create three saved views: Act now (board grouped by agency), Upcoming dates (calendar on Key date), and By state (table grouped by State).

### Notion database: Agencies

One row per registry agency with: name, state, segment, sworn officers, ALPR vendor, camera count, contract end date, account temperature, and last signal date. Update it at the end of each run.

### Daily digest email

Plain text, sent only when at least one signal scores 40 or higher. Subject: `ALPR signals: {n} act now, {m} watch ({date})`.

```
ACT NOW
1. [82] Brookhaven PD: contract_event
   Council work session to discuss camera contract renewal. Vote 2026-12-08.
   "...exact quote..."
   https://source-link

WATCH
...

UPCOMING DATES (next 30 days)
2026-12-08  Brookhaven City Council vote on camera contract

RUN HEALTH
41 sources checked, 2 failed (cobb-commission: timeout; tn-bills: 429)
```

The example above is illustrative, not real data. On Mondays, append a weekly section: account temperature ranking, new bills, and stance shifts.

## Build order

Build in these milestones. Do not start a milestone until the previous one passes its test.

| # | Milestone | Passes when |
| --- | --- | --- |
| 1 | Skeleton: repo layout, config loader with validation, SQLite schema, `run.py` that loads config and writes a run row | `python -m monitor.run --dry-run` exits 0 and prints the count of agencies and sources |
| 2 | One agency end to end: the collector for the first agency's meeting platform, PDF text extraction, dedupe, prefilter, passages | Running twice fetches documents the first time and zero new ones the second; at least one saved fixture yields a passage |
| 3 | Classifier and scoring | A fixture set of 10 hand-labeled passages (5 relevant, 5 not) is classified with at least 9 correct, and scores match hand calculation |
| 4 | Notion sync and digest email | A run creates rows in the Signals database and sends one email; a second run creates no duplicates |
| 5 | Remaining five agencies and the generic HTML collector | All six agencies return documents; one deliberately broken URL is logged and does not stop the run |
| 6 | State legislation and the legislator registry seed script | Bills matching the keywords appear for both states with sponsor names resolved to registry people |
| 7 | News, organization, and vendor feeds | Feed items flow through the same pipeline with no collector-specific code after stage 2 |
| 8 | Meeting video captions, transparency portal snapshots, courts | A portal change between two saved fixtures produces one signal |
| 9 | Stance tracking, account temperature, Agencies database, weekly section | A fixture with two opposing statements from one person produces a "Stance shift" signal |
| 10 | GitHub Actions workflow and README | The scheduled workflow runs green and commits the updated database |

If time is short, milestones 1 through 5 are a working demonstration.

## Guardrails

- **Public sources only.** No logins, no paywall bypass, no private groups, no leaked material.
- **Respect the sites.** Obey `robots.txt` and published terms. If a site forbids automated access, skip it and list it in the README as a manual check.
- **No fabricated data.** Every signal needs a source URL and an exact quote found in the fetched text. Verify in code that the quote appears in the passage; if not, set `needs_review`.
- **People.** Follow the privacy rule in this spec. Never store names of residents who speak in public comment.
- **No employer data.** Nothing from the user's current employer or its customers goes into this system.
- **Cost control.** Cap classification at 300 passages per run. If the cap is hit, process the newest first and log the overflow.
- **Secrets.** Read from environment variables. Never commit keys. Provide `.env.example`.
- **Neutral tone.** Summaries state what was said or decided. No opinions about officials or advocates.

## Environment variables

```
ANTHROPIC_API_KEY=
NOTION_TOKEN=
NOTION_SIGNALS_DB_ID=
NOTION_AGENCIES_DB_ID=
LEGISCAN_API_KEY=
OPENSTATES_API_KEY=
CONGRESS_API_KEY=
COURTLISTENER_TOKEN=
SMTP_USER=
SMTP_APP_PASSWORD=
DIGEST_TO=
```

## Inputs needed from the user

1. The final list of six agencies, or approval of the starter candidates.
2. A Notion integration token and the parent page for the two databases.
3. Free API keys: LegiScan, Open States, Congress.gov, CourtListener.
4. An Anthropic API key.
5. A Gmail address and app password for the digest.
6. A private GitHub repository.

## Verify before coding

- Each agency's meeting platform and listing URL.
- Current endpoints, authentication, and free-tier limits for LegiScan, Open States, Congress.gov, CourtListener, and the Legistar web API.
- Whether each agency has a public transparency portal, and that portal's terms.
- The terms of any news-search feed used; fall back to direct outlet RSS if automated use is not allowed.
- The current low-cost model ID and JSON output options in the Anthropic API.
- The current Notion API version and database property syntax.
