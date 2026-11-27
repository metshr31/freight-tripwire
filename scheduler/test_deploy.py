import json
import os
from pathlib import Path
import stat
import unittest
from types import SimpleNamespace
from deploy import deploy


class DeploymentTests(unittest.TestCase):
    def test_missing_credentials_fail_without_network(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing repository secrets'):
            deploy({}, run=lambda *a, **k: self.fail('must not deploy'))

    def test_secret_transport_permissions_cleanup_and_output_redaction(self):
        captured = []
        def fake_run(command, **kwargs):
            path = Path(command[-1])
            captured.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {'GITHUB_DISPATCH_TOKEN':'dispatch-test-value'})
            self.assertNotIn('GITHUB_DISPATCH_TOKEN', kwargs['env'])
            self.assertNotIn('dispatch-test-value', ' '.join(command))
            self.assertTrue(kwargs['capture_output'])
            return SimpleNamespace(returncode=1, stdout='dispatch-test-value', stderr='private response')
        env={'CLOUDFLARE_API_TOKEN':'cf-test-value','CLOUDFLARE_ACCOUNT_ID':'test-id','GITHUB_DISPATCH_TOKEN':'dispatch-test-value'}
        with self.assertRaises(RuntimeError) as failure:
            deploy(env, run=fake_run)
        self.assertNotIn('dispatch-test-value', str(failure.exception))
        self.assertNotIn('private response', str(failure.exception))
        self.assertFalse(captured[0].exists())
