# Scheduling status and recovery

## Status: repository safeguards deployed; independent trigger requires deployment

The intended schedule is 34 checks each weekday in America/New_York: 08:00,
08:20, 08:40, continuing every 20 minutes through 19:00. Weekday holidays count.
The existing GitHub cron is valid according to GitHub's current official docs,
including IANA timezone/DST support. It remains a best-effort fallback, not a
verified repair of GitHub's trigger delivery.

## Investigation, November 26, 2026

- Default branch is main, workflow is present there and enabled (UI offers
  "Disable workflow"). Repository allows all Actions. Explicit contents: write
  overrides the read-only default token permission; successful state commits
  verify this permission works. No observable approval block or queued backlog
  is present in the inspected history. Public repository, created recently:
  60-day inactivity disabling does not apply.
- Existing global concurrency group `freight-tripwire-state` with cancellation
  disabled already serializes scans. Latest job completes in about one minute.
- API returned 25 run records: 17 manual dispatches, eight schedule records.
  One schedule record has a second attempt. Its November 25 08:22:10 EST start
  is a rerun, not a fresh automatic trigger. The original was created 02:38:50 EST.
- November 25 had 34 expected check times but only one new schedule run record
  created within that day's operating window (17:18:43 EST). This counts trigger
  records, not proven correspondence to particular nominal slots.

| Expected cadence/window (EST) | Observed automatic run creation/start | Gap from prior listed run |
| --- | --- | --- |
| Nov 25: 08:00, 08:20 … 18:40, 19:00 | Nov 25 17:18:43 | — |
| No new check after Nov 25 19:00 | Nov 25 23:14:06 | 5h 55m 23s |
| Next day begins Nov 26 08:00 | Nov 26 02:28:22 | 3h 14m 16s |

Run IDs: 45712124517, 45753905226, 45775561232. Created/start timestamps are
identical for these records. The API does not expose their intended firing
instants. It cannot establish whether a particular occurrence was delayed or
missing, or identify GitHub's internal cause. A timezone typo, slow scan,
concurrency blockage and disabled workflow are not supported by the evidence.
GitHub documents delayed/dropped schedules; ordinary short delays are not an
adequate explanation of this observed pattern.

Official references:
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- https://developers.cloudflare.com/workers/configuration/cron-triggers/
- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/workers/configuration/secrets/

## Admission and state

`schedule_control.py` enforces timezone-aware weekdays and 34 unique slots.
Automatic checks may start less than eight minutes late (19:00's check can
therefore start through 19:07:59). At eight minutes late or later the scan is
refused and must be treated as missed. Old slots are not replayed. No service
is represented as guaranteeing precise timing; GitHub runner queues can still
cause misses even with an independent trigger.

External dispatch sends `automatic: true` and `scheduled_for` as the original
UTC slot. It must not replace a delayed slot with the current time. Duplicate
triggers/retries/reruns hit the same global concurrency group; checkout reads
latest main; a slot claim is committed and PUSHED before source reads or alerts.
Failed claims refuse scanning. A claimed failed/interrupted slot is not retried
automatically: investigate it or use an explicit operator run. This avoids a
second attempt after an uncertain notification delivery. Manual normal scans
consume the current slot when admitted within its grace period; notification
tests retain their existing isolated behavior.

`schedule_status.json` exposes last scan start, completion, exit result, slot,
run URL and last successful check. `scan_history.jsonl` appends start/completion
records. Start records persist before scanning; completion and existing seen,
health and alert files persist afterward. A failed final state push is a failed
workflow; an interrupted run remains visibly running. Existing freshness,
source backoff, baseline seeding, delivery queue and health alert settings are
unchanged. A crash after notification but before durable delivery-state commit
can still duplicate a later alert, as before; this is not exactly-once delivery.

## Independent trigger prepared for Cloudflare Workers Free

`scheduler/worker.mjs` receives UTC cron triggers at minutes 0,20,40 and uses
Intl's America/New_York conversion to select the 34 weekday slots. It dispatches
the existing GitHub workflow; GitHub remains the repository, execution host and
state store. It retries transient HTTP failures up to three times with the same
slot; permanent authentication failures fail visibly. There is no HTTP handler,
public scheduler URL, external notification credential, or database to maintain.
Observability logs contain slot, dispatch acceptance time and HTTP status only.
Dispatch acceptance is not scan success.

Not yet deployed: no connected Cloudflare account or scheduler credential was
available. Deployment must stay on Workers Free; do not enable a paid plan.
Cloudflare trigger propagation may take 15 minutes. Both Cloudflare triggers and
GitHub runner availability can fail or be late; no exact-time guarantee is made.

To deploy after account authorization:
1. Use an existing Cloudflare account on Workers Free.
2. Store a fine-grained GitHub token restricted to this repository, Actions: write
   (plus mandatory metadata read), as Worker secret `GITHUB_DISPATCH_TOKEN`.
   Enter it via the provider's secret input, never chat, tracked files or URLs.
   Creating/granting this credential requires the owner's approval.
3. From `scheduler`, deploy `wrangler.jsonc` with official Wrangler. Keep
   workers_dev and preview_urls disabled. Never add credentials to the config.
4. Inspect accepted dispatches and workflow scans separately. Retain native cron
   as a guarded fallback. Observe three consecutive automatic external-trigger
   scans, approximately 20 minutes apart, including state commits. Verify
   `scheduled_for`, actual starts and completions; don't count manual scans.
5. Revoke/rotate the scoped token when appropriate; expiry becomes a scheduler
   failure and must be detected by the independent watchdog.

## Independent missed-check detection

An hourly check outside GitHub Actions can inspect `scan_history.jsonl` and
`schedule_status.json` through the connected GitHub reader. Compare all slots
whose eight-minute admission grace has ended with completion records; allow up
to the workflow's 15-minute execution timeout before declaring a scan overdue.
Also inspect Actions failures because a final state push can fail after a local
completion record. Missing status, failed runs, stale success or a start without
completion are failures, not healthy quiet news scans. Notify only about a new
or changed failure episode; stay quiet on recovery-free healthy checks. This
watchdog detects GitHub scheduler failure independently but is itself best
effort and has hourly detection latency. It does not perform or replace scans.

## Validation

Local validation: configuration check, 36 Python tests and three Node tests.
Tests include 730 days of weekday/window calculations; both 2026 DST boundaries;
UTC offsets; duplicate/stale triggers; failed durable claims; failed completion;
secret transport in headers; transient dispatch retries; and off-hours silence.
External service dispatch has only mocked network tests until deployment.
Three consecutive automatic executions remain unverified until observed live.

## Live verification results (November 26, 2026, Eastern Standard Time)

Manual verification run:
https://github.com/metshr31/freight-tripwire/actions/runs/45850383988

- Workflow created/started: 10:34:09.
- Scan start recorded: 10:34:23.573; persisted before scanning in commit
  b27672f790f4fc8a0454507ed3aab92a057f68c0.
- Scan completion: 10:35:15.813, exit 0. Workflow completed successfully 10:35:21.
- 311 sources checked, 292 successful, 19 source errors; no source backoff this
  run. Those source errors are recorded by the existing health mechanism and
  are separate from trigger reliability. Zero notification batches, zero pending
  alerts. Existing source and notification settings were preserved.
- Durable completion, health and monitoring state saved in commit
  2bd925bd7ed6b2f592b7c8db6c485ea2e092f0b0. Retrieved status from main to verify
  last_successful_check and successful completion survived the runner.
- The workflow ran all 36 Python and three Node tests successfully.
- An independent ChatGPT watchdog was enabled for weekdays, hourly at :12,
  08:12 through 20:12 Eastern (including DST), starting November 26 at 11:12.
  It audits slots after this deployment and reports here. Its first automatic
  invocation is pending; hourly detection is best effort.
- Automatic schedule verification: **0 of 3 consecutive executions observed**.
  This manual scan does not validate schedule reliability. The operating window
  is open, but the independent trigger is not deployed. No precise timing or
  successful schedule repair is claimed.

## Deployment path after account creation

The owner confirmed creating a Cloudflare account on November 26. Browser login
still returned HTTP 403, so the deployment path is now prepared in
`.github/workflows/deploy-scheduler.yml`; see `scheduler/SETUP.md` for the three
repository secrets required. Those credentials are not yet configured or
verified. Pushes run tests/packaging only. Actual deployment requires a manual
workflow dispatch with `deploy: true`, and cannot occur without all secrets.
The independent trigger is still **not deployed**, and automatic scan timing is
still unverified. No plan upgrade or paid service has been introduced.
