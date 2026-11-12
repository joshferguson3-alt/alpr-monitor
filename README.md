# ALPR Signal Monitor

A daily pass over public records for six metro Atlanta police agencies, Georgia and Tennessee legislation, and a short watchlist of public officials and organizations. It keeps documents that mention automated license plate readers or police surveillance cameras, classifies the passage, scores it, writes it to SQLite and Notion, and emails a ranked digest.

The digest goes only to josh.ferguson3@gmail.com. The program does not log into anything, does not buy data, and does not contact agencies or residents.

## Run

```bash
python -m pip install -r requirements.txt
cp .env.example .env
python -m monitor.run --dry-run
python -m monitor.run --db data/monitor.sqlite
pytest
```

`--dry-run` loads the registry, writes a run row, and prints the agency and source counts. A normal run collects, dedupes, prefilters, classifies, scores, stores, syncs Notion, and sends the digest when a signal scores 40 or higher. One failing source is logged and the run continues. A collector whose API key is unset logs `missing key, skipped`.

`scripts/setup_notion.py` creates the Signals and Agencies databases under `NOTION_PARENT_PAGE_ID` and prints `NOTION_SIGNALS_DB_ID` and `NOTION_AGENCIES_DB_ID`. If Notion returns `object_not_found`, the integration has not been given access to that page. The script reports that and exits 0. `scripts/seed_registry.py` prints review YAML for legislators and does not write `config/`.

## Schedule

`.github/workflows/daily.yml` runs at 6:00 AM Eastern. GitHub cron is UTC, so the workflow starts at 10:00 UTC (6:00 AM during daylight time) and 11:00 UTC (6:00 AM during standard time). A scheduled run whose Eastern hour is not 6 exits immediately. `workflow_dispatch` runs the full set of sources by hand.

After a run, the workflow commits `data/monitor.sqlite`.

## Agencies

Sworn counts were checked on November 12, 2026. Cobb County Police (639 sworn, FBI 2025) and Gwinnett County Police (929 authorized sworn in the 2026 county budget) are outside 41–550 and are not in the registry.

| Agency | Sworn | Segment | Source |
| --- | --- | --- | --- |
| Brookhaven PD | 100 | mid city (41–100) | [State of the City 2026](https://www.brookhavenga.gov/DocumentCenter/View/4293/State-of-the-City-2026-PDF) |
| Dunwoody PD | 66 | mid city | [FBI 2025 count, ORI GA0447100](https://policecensus.com/agencies/ga/dunwoody-police-department) |
| Sandy Springs PD | 152 | 101–550 | [Department site](https://www.sandyspringsgapolice.gov/) |
| Marietta PD | 139 full-time | 101–550 | [About the department](https://www.mariettaga.gov/825/About-Us) |
| Roswell PD | 160 positions, fully staffed | 101–550 | [Department page](https://www.roswellgov.com/government/departments-division/police-department/). Replaces Cobb County PD. |
| Johns Creek PD | 78 current (88 authorized) | mid city | [2025 annual report](https://johnscreekga.gov/app/uploads/2026/05/jcpd-annual-report-2025.pdf). Replaces Gwinnett County PD. |

ALPR vendor is `unknown` until a public page states it. Nothing here is inferred.

## Meeting platforms

| Agency | Platform | What the collector does |
| --- | --- | --- |
| Brookhaven | PrimeGov, [public portal](https://brookhavenga.primegov.com/public/portal) | Skipped. `robots.txt` is `Disallow: /`. |
| Dunwoody | Hyland OnBase Agenda Online | `generic_html` on the meetings search page. The city website itself returned HTTP 403. |
| Sandy Springs | CivicPlus CivicClerk | Public OData API at `sandyspringsga.api.civicclerk.com`. No login. City Council events only. |
| Marietta | CivicPlus Agenda Center | HTML listing plus the agenda file. `robots.txt` allows it with crawl-delay 10 and disallows `/rss.aspx`. |
| Roswell | IQM2 citizens portal | Current agendas. The old Legistar client still serves 2014 events and is not used. Crawl-delay is 60 seconds, so a run takes at most a few agenda files. |
| Johns Creek | Granicus for meetings, city RSS for news | Granicus `robots.txt` is `Disallow: /`, so meeting files are not fetched. The city news feed is. |

Adding another agency is a block in `config/agencies.yaml` and a source block in `config/sources.yaml`. A new meeting platform needs a new collector class.

## Manual checks

These sources are not collected, because the site disallows automated access, blocks this client, or the terms do not allow the feed:

- Brookhaven PrimeGov (`robots.txt` disallows every path). Council agendas have to be opened by hand.
- Johns Creek Granicus (`User-agent: *` / `Disallow: /`). The archive is [ViewPublisher.php?view_id=1](https://johnscreekga.granicus.com/ViewPublisher.php?view_id=1).
- Roswell WordPress uploads (`/wp-content/uploads/` is disallowed). Agendas are taken from IQM2 instead.
- Flock transparency portal (`https://transparency.flocksafety.com/`) returned HTTP 403. No agency in this list had a working public portal URL.
- Google News search RSS is not used. Google's terms do not allow automated access. News comes from the outlet feeds in `config/sources.yaml` (Rough Draft Atlanta, Georgia Recorder, Police1, EFF, and the others listed there).
- CivicPlus RSS feeds are disallowed by those sites' `robots.txt`. Agenda Center HTML is used instead.
- Dunwoody and Smyrna city sites returned HTTP 403 to this client. Smyrna was not needed after Roswell replaced Cobb County PD. Dunwoody meetings are read from OnBase, which did not publish a `robots.txt`.

CivicClerk and the Johns Creek city site send a content signal of `ai-train=no` and `use=reference`, and they allow `User-agent: *`. This program does not train a model on their pages. It keeps a short passage and the source URL.

## Classifier, Notion, mail

The classifier is Claude Haiku 5.5, model id `claude-haiku-5-5`, with JSON schema output and effort `low`. Those were the current low-cost settings in Anthropic's docs on November 12, 2026. A run classifies at most 300 passages, newest first.

Notion uses API version `2026-03-11` and the official `notion-client`. Signals are upserted by Signal ID. The Signals database gets three views when setup can create them: Act now (board by agency), Upcoming dates (calendar on Key date), and By state (table grouped by State).

Mail is SMTP through `smtp.gmail.com:587`. The message is refused unless `DIGEST_TO` is josh.ferguson3@gmail.com. It is sent only when a signal scores 40 or higher. Mondays add account temperature, new bills, and stance shifts.

## Privacy

Named people in the registry are public officials. A resident speaking at a meeting is stored as a count and a stance. The classifier is told to leave resident names null, and the pipeline strips a name when the role is resident or the signal is public sentiment. Every quote is checked against the passage. A quote that is not in the passage sets Needs review.

## Keys

`ANTHROPIC_API_KEY`, `NOTION_TOKEN`, `NOTION_PARENT_PAGE_ID`, `SMTP_USER`, `SMTP_APP_PASSWORD`, and `DIGEST_TO` are repository secrets. `LEGISCAN_API_KEY`, `OPENSTATES_API_KEY`, `CONGRESS_API_KEY`, and `COURTLISTENER_TOKEN` were not available when this was built. Those four collectors log `missing key, skipped` until the secrets exist. LegiScan's free tier is 10,000 queries a month. Open States requires `X-API-KEY`. Congress.gov is `https://api.congress.gov/v3/bill`. CourtListener is REST v4 with `Authorization: Token`.

Copy `.env.example` to `.env`. Do not commit `.env`.
