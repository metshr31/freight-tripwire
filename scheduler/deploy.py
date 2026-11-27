"""Deploy via GitHub Secrets, without printing tokens or publishing secret files."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def deploy(environ=None, run=subprocess.run):
    env = dict(os.environ if environ is None else environ)
    required = ('CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'GITHUB_DISPATCH_TOKEN')
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise RuntimeError('Missing repository secrets: ' + ', '.join(missing).replace('GITHUB_DISPATCH_TOKEN', 'TRIPWIRE_DISPATCH_TOKEN'))
    # The dispatch credential goes only into the encrypted Worker secret binding.
    # Remove it from Wrangler's process environment to avoid implicit dev vars.
    dispatch_token = env.pop('GITHUB_DISPATCH_TOKEN')
    env['CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV'] = 'false'
    env['CLOUDFLARE_INCLUDE_PROCESS_ENV'] = 'false'
    with tempfile.TemporaryDirectory(prefix='tripwire-deployment-') as directory:
        secrets = Path(directory) / 'secrets.json'
        secrets.touch(mode=0o600)
        secrets.write_text(json.dumps({'GITHUB_DISPATCH_TOKEN': dispatch_token}))
        result = run(['npx', '--no-install', 'wrangler', 'deploy', '--secrets-file', str(secrets)],
                     env=env, text=True, capture_output=True)
        if result.returncode:
            # Avoid leaking third-party response bodies or secret values into logs.
            raise RuntimeError(f'Cloudflare deployment failed (exit {result.returncode}); verify account ID and scoped API-token permissions')
    print('Worker deployed with encrypted dispatch secret. No secret values logged.')


if __name__ == '__main__':
    try:
        deploy()
    except RuntimeError as error:
        raise SystemExit(str(error))
