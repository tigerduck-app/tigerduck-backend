# launchd services

Version-controlled launchd plists for TigerDuck services that run NATIVE on the host (not inside Docker): the course announcement classifier and the nightly off-site backup. The backend runs in Docker compose.

## LLM

The bulletin classifier's LLM is not part of this repository. The backend calls the OpenAI-compatible server that `TIGERDUCK_LLM_BASE_URL`, `TIGERDUCK_LLM_MODEL` and `TIGERDUCK_LLM_API_KEY` in `.env` name; the README's LLM section lists what that server must support.

In production it is a service on the host shared with other apps: `~/selfhost/llm`, launchd label `tw.xinshou.llm`, vllm-mlx serving `mlx-community/Qwen3.5-9B-6bit` as `qwen3.5-9b` on port 40001. It runs natively because Docker Desktop on Mac can't expose the Metal GPU to containers. It requires a Bearer key: the value of `TIGERDUCK_LLM_API_KEY`.

```bash
# From the host
curl -sS -H "Authorization: Bearer <key>" http://localhost:40001/v1/models | jq

# From the backend container, with the settings it runs with
docker exec tigerduck-internal python -c \
  "import os, urllib.request; r = urllib.request.Request(os.environ['TIGERDUCK_LLM_BASE_URL'] + '/models', headers={'Authorization': 'Bearer ' + os.environ['TIGERDUCK_LLM_API_KEY']}); print(urllib.request.urlopen(r, timeout=3).status)"
```

A 401 means the key in `.env` is not the one the server expects.

## ai.tigerduck.classifier

The course announcement classifier (`tigerduck-app/announcement-classifier`, `scripts/serve.py`) on port **40002**. It runs on the CPU; it stays outside Docker because its model would add over a gigabyte to the backend image. The backend calls it at `TIGERDUCK_ANNOUNCEMENT_CLASSIFIER_URL` (`http://host.docker.internal:40002`); with that unset, course announcements are not polled.

### Install or upgrade

`deploy/install.sh <tag>` in the classifier repository downloads that release's model into `model/`, checks it against the release's `SHA256SUMS`, writes the tag to `model/RELEASE` (which `/health` reports as `release`), builds `.venv` and runs the service test, which prints `serve.py ok`. It needs `gh` signed in and `uv`.

```bash
git clone https://github.com/tigerduck-app/announcement-classifier.git ~/models/tigerduck/announcement-classifier
cd ~/models/tigerduck/announcement-classifier
deploy/install.sh model-2026-11-15        # the release tag

cd /Users/xinshoutw/selfhost/Docker/tigerduck-backend
ln -sf "$(pwd)/deploy/launchd/ai.tigerduck.classifier.plist" \
       ~/Library/LaunchAgents/ai.tigerduck.classifier.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.tigerduck.classifier.plist
```

For a later release, `git pull` and `deploy/install.sh <new tag>` in the classifier checkout, then `launchctl kickstart -k gui/$UID/ai.tigerduck.classifier`.

### Verify

```bash
curl -sS http://localhost:40002/health | jq '.release, .threshold'

docker exec tigerduck-internal python -c \
  "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:40002/health', timeout=3).status)"
```

### Tear down

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/ai.tigerduck.classifier.plist
rm ~/Library/LaunchAgents/ai.tigerduck.classifier.plist
```

Logs: `~/Library/Logs/tigerduck-classifier.{out,err}.log`. The service never logs request bodies.

## ai.tigerduck.backup

Runs `deploy/backup/tigerduck-backup.sh nightly` at 04:30: an encrypted `pg_dump` to Cloudflare R2, and on Sundays a restore drill into a throwaway container. Install, verify and restore steps are in MIGRATE.md under "Upgrading to 3.4.0". Log: `~/Library/Logs/tigerduck-backup.log`.
