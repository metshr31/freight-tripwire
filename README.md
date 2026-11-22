# Freight Tripwire — deployment package (2026-11-22)

A public-source lead monitor for trucking, LTL, intermodal, rail, ports, airfreight, regulatory notices, and the UP–NS merger docket, **FD 36873**. Deliveries use [ntfy](https://ntfy.sh), with GitHub Actions as the scheduler.

**Important:** This is a lead detector, not an independently verified fact-checker or a comprehensive crawler of every watched company's website. It cannot guarantee alerts before other news publications. Treat community/forum posts as **unverified tips**. Always verify underlying documents before publication.

## Contents

- `watchlist.json`: 195 supplied company-name/alias entries, 15 supplied feed URLs, official STB pages, FD 36873's dedicated docket feed, signal terms and source exclusions.
- `watcher.py`: polling, RSS/Atom parsing, official STB docket filtering, per-source quiet baseline, duplicate handling, alert triage, queue-and-retry, deliveries, health status.
- `.github/workflows/freight-watcher.yml`: timezone-aware schedule, permissions, non-overlapping runs, Python setup, manual test-push option and durable commit.
- `tests/test_watcher.py`: offline unit tests.
- `requirements.txt`: Beautiful Soup for parsing HTML.

## Setup in GitHub Work/browser

1. Create the **public** repository `metshr31/freight-tripwire`, initialized with a README, or use a repository you intentionally select. Do not publish your ntfy topic or credentials in repository files.
2. Upload the package contents to the repository root, preserving `.github/workflows/freight-watcher.yml` and replacing the initialized README.
3. Go to **Settings → Secrets and variables → Actions → New repository secret**. Create:
   - `NTFY_TOPIC` = the private-by-obscurity topic name already subscribed to on the phone. (No `https://ntfy.sh/` prefix.)
   - `SEC_USER_AGENT` = an identified, real SEC user agent, for example `Freight Pulse (your-email@example.com)` **but replace the example with a working newsroom contact address**. The value is kept out of the public repository. See the SEC's fair-access policy.
4. Under **Actions → Freight and STB Tripwire → Run workflow**, select `test_push = true` and run. Confirm the workflow finishes and the **notification appears on the phone**. GitHub success means ntfy accepted the request; only the user can confirm device delivery.
5. Run workflow again with `test_push = false`. The **first successful fetch of each individual source** seeds older items without notifications. The repository should gain `seen_items.json` and `health.json`; `alerts.jsonl` starts after a real new qualifying lead is successfully published.
6. Check `health.json` and the Actions logs for bad/retired feeds, SEC blocks, access restrictions, malformed responses and source failures. Replace broken sources only after checking their actual current endpoints.
7. Wait for a genuine new source event, or perform a deliberate controlled test in a temporary test source. Do not claim full end-to-end monitoring is validated until a new item is detected, delivered, logged and survives a subsequent run without duplication.

## Schedule

- **Every 10 minutes** Monday–Friday, 7:03 a.m. through 6:53 p.m. Eastern.
- **Hourly** around the clock including weekends, for some overnight/weekend coverage.
- GitHub's `timezone` field adjusts for Eastern daylight-saving changes.
- Scheduled GitHub Actions jobs are **best effort**, not guaranteed to start precisely on time. Monitor workflow failures. A stopped GitHub schedule cannot alert on its own; a separate external heartbeat is required for that level of assurance.

## Alert logic

- **P1 (priority 4)**: major event signal involving a named company; STB merger docket decisions.
- **P2 (priority 3)**: material event or watched-company SEC 8-K; regulatory notice. Routine FD 36873 filings are batched to reduce notification noise.
- **TIP (priority 3)**: company + major freight event discussed on social/forum sources, **explicitly unverified**.
- Company mention alone is not generally sufficient. Common-name matches such as `Amazon` require freight context. News mentioning FreightWaves or SONAR is filtered. These heuristics are imperfect, and false positives/negatives remain possible.
- Each successful publish is saved to `alerts.jsonl` with an original link and UTC notification timestamp.
- **Failed pushes remain queued** until later runs. Push success means ntfy server acceptance, not guaranteed handset delivery; deduplication can still fail if a GitHub commit fails after a successful push.
- Source-specific quiet seeding prevents a new source from generating an old-item alert storm.
- After three consecutive failed checks of a source, the watcher attempts a consolidated warning no more frequently than once every 12 hours.

## Regulatory coverage and caveats

- FD 36873: purpose-built Docket Yard Atom feed, plus official STB filings, decisions and environmental comments pages. The reader loads each page’s public dynamic table endpoint and checks its latest 500 records, including subdockets. This is a bounded recent-record check; long outages or exceptionally busy periods can leave gaps. Docket Yard is independent and the underlying STB filing is authoritative. Docket Yard itself notes coverage can vary and recommends the live feed for changes.
- SEC 8-K: polls 3 pages of the general SEC Atom feed each run (up to 300 entries), matching the company names in headlines/summaries. **This is not guaranteed comprehensive EDGAR coverage**. To monitor all relevant 8-Ks, add verified CIK identifiers and per-filer submissions monitoring in a future iteration.
- Federal Register: supplied STB, FMCSA and FRA RSS URLs. Verify their current validity in Actions; feed endpoints can change.
- EIA, press releases, trucker forums and Reddit: RSS feeds only, subject to their respective availability, filtering and rate policies. A working feed does not independently verify any claim made in an item.
- The 195 entries include spelling/name variants, **not 195 independently polled company websites**. For truly primary-source coverage of each carrier, add separately verified IR/press/operational URLs and test each one.

## Local commands

```bash
python -m pip install -r requirements.txt
python watcher.py --check-config
python -m unittest discover -s tests -v
# Test push only when NTFY_TOPIC is deliberately set in your own environment:
python watcher.py --test-push
# Read live sources without saving state or sending alerts:
python watcher.py --dry-run
```

### Troubleshooting

- **No notification at baseline:** expected. Baseline is deliberately quiet. Use manual `test_push = true` for a real push.
- **`SEC_USER_AGENT missing`:** add the secret using an identified contact. SEC coverage will show a failure until then.
- **Repeated RSS 403/429:** remote service blocked or throttled access; check conditions of use. Do not aggressively retry or circumvent restrictions.
- **Bad STB HTML table:** site layout changed. Docket Yard feed provides an independent secondary alerting path; adapt the official STB parser after inspecting live HTML.
- **Workflow doesn't run on schedule:** confirm Actions are enabled, the workflow is on the default branch, scheduled workflows haven't been auto-disabled, and check GitHub's scheduling delays.
- **Duplicate pushes:** inspect whether the bot failed to commit `seen_items.json` after delivery.
- **Public repo privacy:** alert history, watched entities and detection terms will be visible publicly. Secrets are not included in files. Public ntfy.sh topics are not access-controlled merely by a hard-to-guess name.

## Source reference URLs

- GitHub: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- SEC EDGAR access: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- STB filings: https://www.stb.gov/proceedings-actions/filings/
- STB decisions: https://www.stb.gov/proceedings-actions/decisions/
- Docket Yard FD 36873 Atom feed: https://docketyard.org/d/FD-36873/feed
- Docket Yard API/coverage: https://docketyard.org/api
- ntfy publishing: https://docs.ntfy.sh/publish/
