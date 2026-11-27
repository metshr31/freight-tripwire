# Finish independent scheduler deployment

The browser login to Cloudflare returned HTTP 403 on November 26. Your account
exists, but this session could not authenticate. This deployment path uses
Cloudflare's documented GitHub Actions integration and does not require that
browser session. It does not change your Cloudflare plan or purchase anything.

## One-time account setup

Use your existing signed-in Cloudflare account and GitHub account to add these
**repository Actions secrets** to `metshr31/freight-tripwire`:

| GitHub secret name | Value | Access |
| --- | --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID | Account identifier; keep in Secrets with the other settings |
| `CLOUDFLARE_API_TOKEN` | Scoped Cloudflare deployment API token | Limit to your account and Workers deployment; use the documented Edit Cloudflare Workers policy, removing unrelated products/zones where the UI allows. This Worker needs scripts, schedules and encrypted secrets; no DNS, domains, KV, R2 or billing changes. |
| `TRIPWIRE_DISPATCH_TOKEN` | Fine-grained GitHub token | Only `metshr31/freight-tripwire`, repository Actions: read/write plus mandatory Metadata: read. No contents-write scope. Choose an expiration and plan to rotate it before expiry. |

Add them at:
https://github.com/metshr31/freight-tripwire/settings/secrets/actions

Never paste tokens into chat, source files, workflow inputs or URLs. Tokens go
only into the providers' secret fields. This creates security-sensitive access:
the Cloudflare token lets the deployment job update Workers in its allowed
scope, while the GitHub token lets the Worker dispatch Actions for this repo.
The deployment helper transfers the latter directly to an encrypted Worker
binding named `GITHUB_DISPATCH_TOKEN`. It uses an owner-only temporary file,
deletes it after use, and suppresses third-party output containing secret values.

Official Cloudflare setup instructions:
https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/

## Deploy

1. Open Actions > **Deploy independent scheduler**.
2. Select **Run workflow**, leave branch as `main`, and check **deploy**.
3. Run it. This deploys the prepared Worker and its cron triggers. Keep Workers
   Free; the workflow does not enable subscriptions or request payment.
4. The default unchecked **deploy** setting runs tests and packaging only.
   Pushes to scheduler code also validate only; state commits do not trigger it.

The workflow uses pinned Wrangler 4.168.0 with a dependency lockfile. It does not
receive `NTFY_TOPIC` or `SEC_USER_AGENT`, alter sources, scan news, or mutate
monitoring state. Cloudflare's account ID/API token stay in GitHub Secrets;
only the scoped dispatch credential is uploaded into the encrypted Worker secret.

## Verify

Deployment success alone is not timing verification. Allow up to 15 minutes for
Cloudflare trigger propagation, then observe three consecutive automatic scans
in the next operating window with intended slots, actual start/completion,
successful durable commits and roughly 20-minute intervals. The source scanner
retains its 34-slot weekday/DST guard and duplicate suppression. GitHub cron is
still only a guarded fallback. Cloudflare dispatch acceptance does not guarantee
GitHub runner start times; missed checks remain visible to the independent audit.

If the operating window is closed, verification remains pending until the next
weekday (including weekday holidays). The next window after the November 26
late-evening setup is Friday, November 27, 08:00–19:00 America/New_York.
