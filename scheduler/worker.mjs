// Cron-only Worker: no public HTTP handler, URL credentials, or notification secrets.
const formatter = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', weekday: 'short', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

export function intendedSlot(time) {
  const date = new Date(time);
  const p = Object.fromEntries(formatter.formatToParts(date).map(x => [x.type, x.value]));
  const minutes = Number(p.hour) * 60 + Number(p.minute);
  if (['Sat', 'Sun'].includes(p.weekday) || minutes < 480 || minutes > 1140 || minutes % 20 !== 0) return null;
  date.setUTCSeconds(0, 0);
  return date.toISOString();
}

export async function dispatch(time, env, request = fetch) {
  const slot = intendedSlot(time);
  if (!slot) return 'outside-window';
  if (!env.GITHUB_DISPATCH_TOKEN) throw new Error('Missing GITHUB_DISPATCH_TOKEN secret');
  const url = 'https://api.github.com/repos/metshr31/freight-tripwire/actions/workflows/freight-watcher.yml/dispatches';
  // A timeout can occur after GitHub accepts a request. Retries carry the same
  // intended slot; GitHub concurrency + committed slot claims suppress replays.
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await request(url, {
        method: 'POST',
        headers: {Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          Accept: 'application/vnd.github+json', 'User-Agent': 'FreightTripwireScheduler',
          'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'},
        body: JSON.stringify({ref: 'main', inputs: {automatic: true, scheduled_for: slot}}),
        signal: AbortSignal.timeout(15000),
      });
      if (response.ok) {
        console.log(JSON.stringify({slot, dispatch_accepted_at: new Date().toISOString(), status: response.status}));
        return 'dispatched';
      }
      if (response.status < 500 && response.status !== 429) {
        throw new Error(`Non-retryable dispatch status ${response.status}`);
      }
    } catch (error) {
      // Do not print response bodies, headers, environment values or raw errors.
      if (String(error.message).startsWith('Non-retryable')) throw error;
    }
  }
  throw new Error(`Dispatch failed for ${slot}; inspect GitHub and Worker status`);
}

export default {
  async scheduled(controller, env) {
    // Use the ORIGINAL intended time; a delayed invocation must not impersonate
    // a newer slot. The workflow rejects arrivals >=8 minutes late.
    await dispatch(controller.scheduledTime, env);
  },
};
