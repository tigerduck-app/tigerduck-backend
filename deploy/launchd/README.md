# launchd services

Version-controlled launchd plists for TigerDuck services that run NATIVE on the host (not inside Docker): llama-server, the course announcement classifier and the nightly off-site backup. The backend runs in Docker compose.

## ai.tigerduck.llm

`llama-server` serving `ggml-org/gemma-4-E4B-it-GGUF` on port **40001** for the bulletin classifier. Lives outside Docker because Docker Desktop on Mac can't expose Metal GPU to containers.

### Install (once)

```bash
cd /Users/xinshoutw/selfhost/Docker/tigerduck-backend   # the repo checkout on the host

# Symlink so edits to the repo copy auto-apply on next launchctl kickstart.
ln -sf "$(pwd)/deploy/launchd/ai.tigerduck.llm.plist" \
       ~/Library/LaunchAgents/ai.tigerduck.llm.plist

# Boot it. `bootstrap gui/$UID` registers the service in the user's
# graphical session; survives logout via "KeepAlive".
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.tigerduck.llm.plist

# First launch downloads ~4GB of GGUF weights into the Hugging Face cache
# (~/.cache/huggingface/hub/ by default). Be patient.
tail -f ~/Library/Logs/tigerduck-llm.err.log
```

### Verify

```bash
# Process listening?
lsof -iTCP:40001 -sTCP:LISTEN

# LLM answering?
curl -sS http://localhost:40001/v1/models | jq

# Backend container can reach it via host.docker.internal?
docker exec tigerduck-internal python -c \
  "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:40001/v1/models', timeout=3).status)"
```

### Tear down

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/ai.tigerduck.llm.plist
rm ~/Library/LaunchAgents/ai.tigerduck.llm.plist
```

### After editing the plist

```bash
launchctl kickstart -k gui/$UID/ai.tigerduck.llm
# or if you changed the file structure:
launchctl bootout gui/$UID ~/Library/LaunchAgents/ai.tigerduck.llm.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.tigerduck.llm.plist
```

### Troubleshooting

| Symptom | Check |
|---|---|
| Service flaps / KeepAlive restart loop | `tail -100 ~/Library/Logs/tigerduck-llm.err.log` |
| Container can't reach llama | `docker exec tigerduck-internal curl -sS http://host.docker.internal:40001/v1/models` — if 0 bytes, the `--host 0.0.0.0` line in the plist got edited to `127.0.0.1` |
| High CPU / fan noise | Either --threads is too high for your Mac, or a runaway prompt > `_MAX_BODY_CHARS` is being sent |

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
