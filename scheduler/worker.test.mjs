import {test} from 'node:test';
import assert from 'node:assert/strict';
import {dispatch, intendedSlot} from './worker.mjs';

test('34 dispatch slots per weekday across winter, summer and DST changes', () => {
  for (const day of ['2026-03-06','2026-03-09','2026-10-30','2026-11-02','2026-11-26']) {
    let slots = [];
    // ET day 00:00 to next 00:00; these sample weekdays are each 24 hours long.
    const first = Date.parse(`${day}T05:00:00Z`) - (day >= '2026-03-09' && day <= '2026-10-30' ? 3600000 : 0);
    for (let t = first; t < first + 86400000; t += 60000) if (intendedSlot(t)) slots.push(t);
    assert.equal(slots.length, 34, day);
    assert.ok(slots.slice(1).every((t, i) => t - slots[i] === 1200000));
    assert.equal(new Date(slots[0]).getUTCHours(), day >= '2026-03-09' && day <= '2026-10-30' ? 12 : 13);
  }
  assert.equal(intendedSlot(Date.parse('2026-11-28T13:00:00Z')), null);
});
test('dispatch uses secret header and exact slot, without secret URL/body', async () => {
  const secret = 'test-only-credential';
  let calls = 0;
  assert.equal(await dispatch(Date.parse('2026-11-26T13:00:00Z'), {GITHUB_DISPATCH_TOKEN: secret}, async (url, init) => {
    calls++;
    assert.ok(!url.includes(secret));
    assert.ok(!init.body.includes(secret));
    assert.equal(init.headers.Authorization, `Bearer ${secret}`);
    assert.deepEqual(JSON.parse(init.body), {ref:'main',inputs:{automatic:true,scheduled_for:'2026-11-26T13:00:00.000Z'}});
    return {ok:true,status:204};
  }), 'dispatched');
  assert.equal(calls, 1);
});
test('off-hours quiet, failed dispatch visible, transient failures retried', async () => {
  const env = {GITHUB_DISPATCH_TOKEN:'test'};
  let count = 0;
  await dispatch(Date.parse('2026-11-26T13:00:00Z'), env, async () => ({ok:++count===3,status:count===3?204:503}));
  assert.equal(count,3);
  await assert.rejects(dispatch(Date.parse('2026-11-26T13:00:00Z'),env,async()=>({ok:false,status:401})),/401/);
  assert.equal(await dispatch(Date.parse('2026-11-26T07:20:00Z'), env,()=>{throw Error('should not fetch')}),'outside-window');
});
