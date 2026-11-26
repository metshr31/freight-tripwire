import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
import schedule_control as sc


class ScheduleTests(unittest.TestCase):
    def test_every_day_of_two_years(self):
        day = date(2026, 1, 1)
        for i in range(730):
            d = day + timedelta(days=i)
            slots = sc.slots(d)
            self.assertEqual(len(slots), 34 if d.weekday() < 5 else 0)
            if slots:
                self.assertEqual(slots[0].strftime('%H:%M'), '08:00')
                self.assertEqual(slots[-1].strftime('%H:%M'), '19:00')
                self.assertTrue(all((b-a).total_seconds() == 1200 for a,b in zip(slots,slots[1:])))

    def test_dst_boundaries(self):
        for d, hour in [('2026-03-06',13), ('2026-03-09',12),
                        ('2026-10-30',12), ('2026-11-02',13)]:
            self.assertEqual(sc.slots(date.fromisoformat(d))[0].astimezone(timezone.utc).hour, hour)

    def test_admission_and_duplicates(self):
        current = datetime.fromisoformat('2026-11-26T13:02:00+00:00')
        slot, _ = sc.admit(current, '2026-11-26T13:00:00Z')
        self.assertEqual(slot, '2026-11-26T13:00:00+00:00')
        self.assertIsNone(sc.admit(current, last=slot)[0])
        self.assertIsNone(sc.admit(current, '2026-11-25T13:00:00Z')[0])
        self.assertIsNone(sc.admit(current, '2026-11-26T13:00:00')[0])
        self.assertIsNone(sc.admit(current, 'bad')[0])

    def test_window_and_grace(self):
        for t in ['2026-11-26T12:59:59Z','2026-11-26T13:08:00Z',
                  '2026-11-27T00:08:00Z','2026-11-28T13:00:00Z',
                  '2026-11-26T07:28:22Z']:
            self.assertIsNone(sc.admit(datetime.fromisoformat(t))[0], t)
        self.assertIsNotNone(sc.admit(datetime.fromisoformat('2026-11-27T00:07:59Z'))[0])

    def test_lifecycle_and_failed_slot_cannot_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sc, 'STATUS', Path(directory)/'status.json'), patch.object(sc, 'HISTORY', Path(directory)/'history.jsonl'), patch.dict(os.environ, {'GITHUB_ACTIONS':'false'}):
                current = datetime.fromisoformat('2026-11-26T13:01:00Z')
                self.assertTrue(sc.begin(current))
                self.assertFalse(sc.begin(current))
                sc.finish(1)
                doc = json.loads(sc.STATUS.read_text())
                self.assertEqual(doc['last_scan']['status'], 'failure')
                self.assertNotIn('last_successful_check', doc)
                self.assertFalse(sc.begin(current))
                self.assertTrue(sc.begin(current + timedelta(minutes=20)))
                sc.finish(0)
                self.assertIn('last_successful_check', json.loads(sc.STATUS.read_text()))

    def test_failed_claim_prevents_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sc, 'STATUS', Path(directory)/'status.json'), patch.object(sc, 'HISTORY', Path(directory)/'history.jsonl'), patch.dict(os.environ, {'GITHUB_ACTIONS':'true'}), patch.object(sc, 'persist', side_effect=RuntimeError('push failed')):
                with self.assertRaises(RuntimeError):
                    sc.begin(datetime.fromisoformat('2026-11-26T13:01:00Z'))
