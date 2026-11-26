"""Slot admission and durable scan lifecycle; no notification/source policy changes."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import os
from pathlib import Path
import subprocess
import sys

ET = ZoneInfo('America/New_York')
UTC = timezone.utc
STATUS = Path('schedule_status.json')
HISTORY = Path('scan_history.jsonl')


def slots(day):
    """Exactly 34 timezone-aware instants on each weekday, including 19:00."""
    if day.weekday() >= 5:
        return []
    first = datetime(day.year, day.month, day.day, 8, tzinfo=ET)
    return [first + timedelta(minutes=20 * i) for i in range(34)]


def admit(current, requested='', last=None):
    current = current.astimezone(UTC)
    local = current.astimezone(ET)
    due = [s for s in slots(local.date()) if s <= local]
    if not due:
        return None, 'outside operating window'
    slot = due[-1].astimezone(UTC)
    # A bounded late-start allowance, including the final 19:00 check. Never
    # replay overnight/backlogged checks, and never backfill old notifications.
    if not 0 <= (current - slot).total_seconds() < 8 * 60:
        return None, 'outside eight-minute admission grace'
    if requested:
        try:
            target = datetime.fromisoformat(requested.replace('Z', '+00:00'))
            if target.tzinfo is None or target.astimezone(UTC) != slot:
                return None, 'stale or invalid requested slot'
        except ValueError:
            return None, 'invalid requested slot'
    key = slot.isoformat()
    if last and last >= key:
        return None, 'slot already claimed'
    return key, 'accepted'


def save(doc):
    temp = STATUS.with_suffix('.tmp')
    temp.write_text(json.dumps(doc, indent=2, sort_keys=True) + '\n')
    temp.replace(STATUS)


def persist(message):
    """A claim must reach GitHub before any source/notification side effects."""
    subprocess.run(['git', 'add', '--', str(STATUS), str(HISTORY)], check=True)
    subprocess.run(['git', 'commit', '-m', message], check=True)
    for _ in range(3):
        if subprocess.run(['git', 'pull', '--rebase', 'origin', 'main']).returncode == 0:
            if subprocess.run(['git', 'push', 'origin', 'HEAD:main']).returncode == 0:
                return
    raise RuntimeError('Durable scan claim could not be pushed; scan refused')


def begin(current=None, requested='', event='schedule', manual=False):
    current = current or datetime.now(UTC)
    doc = json.loads(STATUS.read_text()) if STATUS.exists() else {'version': 1}
    slot, reason = admit(current, requested, doc.get('last_claimed_slot'))
    if manual:
        # Explicit operator scans remain possible at any time. Within a slot's
        # grace period they consume that slot, protecting automatic scans too.
        if reason == 'slot already claimed':
            return False
        reason = 'manual verification'
    elif not slot:
        print('Scan skipped:', reason)
        return False
    record = {'slot': slot, 'started_at': current.isoformat(), 'completed_at': None,
              'status': 'running', 'event': event,
              'run_url': os.getenv('SCAN_RUN_URL', ''), 'exit_code': None}
    if slot:
        doc['last_claimed_slot'] = slot
    doc.update({'last_scan': record})
    save(doc)
    with HISTORY.open('a') as stream:
        stream.write(json.dumps(record, sort_keys=True) + '\n')
    if os.getenv('GITHUB_ACTIONS') == 'true':
        persist('Record scan start and claim scheduling slot')
    print('Scan admitted:', reason, slot or 'operator scan')
    return True


def finish(code):
    doc = json.loads(STATUS.read_text())
    record = doc['last_scan']
    record.update(completed_at=datetime.now(UTC).isoformat(),
                  status='success' if code == 0 else 'failure', exit_code=code)
    if code == 0:
        doc['last_successful_check'] = record['completed_at']
        if record['slot']:
            doc['last_successful_slot'] = record['slot']
    save(doc)
    with HISTORY.open('a') as stream:
        stream.write(json.dumps(record, sort_keys=True) + '\n')


def main():
    action = sys.argv[1]
    if action == 'begin':
        accepted = begin(requested=os.getenv('SCHEDULED_FOR', ''),
                         event=os.getenv('GITHUB_EVENT_NAME', 'manual'),
                         manual=os.getenv('MANUAL_SCAN') == 'true')
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f"scan={'true' if accepted else 'false'}\n")
    elif action == 'finish':
        finish(int(sys.argv[2]))
    elif action == 'summary':
        if STATUS.exists():
            doc = json.loads(STATUS.read_text())
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                stream.write('### Durable scan status\n```json\n' + json.dumps(doc, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
