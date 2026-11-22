# Freight Tripwire

Direct company news, investor-relations releases, customer advisories, and freight/regulatory leads, delivered through ntfy.

[![Freight and STB Tripwire](https://github.com/metshr31/freight-tripwire/workflows/Freight%20and%20STB%20Tripwire/badge.svg)](https://github.com/metshr31/freight-tripwire/actions/workflows/freight-watcher.yml)

## Coverage

[**COVERAGE.md**](COVERAGE.md) maps every watched company name/alias to its verified direct endpoints and explicitly lists remaining gaps. [**primary_sources.json**](primary_sources.json) contains the actual endpoints, parser types, source roles, verification dates and sample headlines. Some aliases and subsidiaries share parent-company releases. A company-name match in a shared news feed is additional coverage, and does not count as a working direct company source.

Direct sources include all six Class I railroads, major public truckload/LTL carriers, private carriers, brokers and logistics companies. The railroad readers cover BNSF customer notifications; UP intermodal/pricing and customer announcements; CSX investor releases (customer news/service bulletins currently return HTTP 403); NS customer alerts/news releases; CN news including gate-hour notices; and CPKC's official investor-relations release feed. CPKC's authenticated customer portal is outside the public reader's reach.

Other sources remain: FD 36873's Docket Yard feed; official STB filings, decisions and environmental comments; STB/FMCSA/FRA Federal Register feeds; EIA; PR Newswire; Business Wire; GlobeNewswire; Transport Topics; and attempted community/forum RSS feeds. FreightWaves/SONAR mentions are excluded. Community posts are labeled unverified tips. Reddit/TruckingBoards access can fail; see runtime health. SEC checks remain unconfigured until an identified contact is approved and stored as a secret.

## Speed and alert handling

- A five-minute schedule is configured **24/7**, including nights, weekends and holidays, at minutes 02, 07, 12, 17, 22, 27, 32, 37, 42, 47, 52 and 57.
- Direct operational notices are fetched first; direct news/IR sources follow. Sources run concurrently with bounded workers and timeouts.
- A new qualifying direct-company item is sent **as soon as that source finishes**, without waiting for slow social feeds. Some per-run delivery capacity is reserved for urgent items.
- Source excerpts and original URLs accompany alerts; publication times are included when supplied. A registered company source supplies the company identity even when its headline omits the company name.
- Content revisions at the same advisory URL can trigger an update. Other items are deduplicated by their canonical original URL. Separate URLs for syndicated versions can still generate duplicates.
- Every new source gets a **quiet initial baseline**. Adding sources does not send a backlog of old stories.
- Failed notifications remain queued for retry. Local state is saved immediately after direct deliveries; GitHub commits state at the end of the run. A runner/commit failure can still cause duplicate delivery.
- HTTP 403/429 sources back off for 30 minutes. Repeated other direct-source failures back off for 15 minutes; failures remain visible.

GitHub scheduled starts are **best effort**. A five-minute cron is not a guaranteed five-minute delivery SLA and cannot guarantee beating every rival publication. This detects published leads; it does not independently verify every assertion or access private customer emails/portals. Bounded source windows can miss items during long outages or very busy periods.

## Live status and durable state

Latest live check: November 22, 2026, 3:55 p.m. Eastern. 234 direct endpoints succeeded, covering 161 of 195 company names/aliases. Automatic scheduled starts have not yet been observed; successful manual runs alone do not prove unattended operation. All 18 unit tests and the isolated primary-source/real-ntfy pipeline test passed. SEC remains unconfigured; Reddit and TruckingBoards checks failed.

[**health.json**](health.json) reports each source's last success, failure, article count, backoff, runtime, working-primary count, and `company_names_without_live_primary`. Discovery verification does not guarantee continued runtime access. An error or an empty/unparseable page is not counted as a working reader; a valid official JSON/table response can legitimately contain zero matching records.

`seen_items.json` preserves baselines, deduplication, delivery queues and advisory revisions. `alerts.jsonl` records delivered news leads when there are any. Runs are serialized and checkout the current `main` branch so a queued run does not replay stale monitoring state. Source-health notifications are throttled to once per 12 hours.

## Secrets

The public repository contains no ntfy topic or credentials. Repository Actions secrets:

- `NTFY_TOPIC`: the subscribed topic name, with no URL prefix.
- `SEC_USER_AGENT`: an identified requester/contact, configured only with the account owner's approval.

The ephemeral `GITHUB_TOKEN` handles state commits. Fork pull requests do not run this scheduled/manual workflow or receive its secrets.

## Validation and manual controls

```bash
python -m pip install -r requirements.txt
python watcher.py --check-config
python -m unittest discover -s tests -v
python watcher.py --dry-run
```

Under **Actions → Freight and STB Tripwire → Run workflow**:

- Default inputs: poll live sources, quietly seed new sources, deliver new qualifying leads, and commit state.
- `test_push=true`: send a real ntfy delivery test without changing monitoring baselines.
- `pipeline_test=true`: use an isolated synthetic source to test the real primary parser, quiet baseline, new-item detection, ntfy delivery, persistence and duplicate suppression. Exactly one clearly labeled test notification is expected. Live news baselines are untouched.

Phone receipt has been confirmed by the subscriber for the original GitHub delivery test. A later server-accepted publish is not independent proof that a particular handset displayed it.
