# launchd services

Version-controlled launchd plists for TigerDuck services that run NATIVE on the host (not inside Docker): the nightly off-site backup. The backend runs in Docker compose.

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

## ai.tigerduck.backup

Runs `deploy/backup/tigerduck-backup.sh nightly` at 04:30: an encrypted `pg_dump` to Cloudflare R2, and on Sundays a restore drill into a throwaway container. Install, verify and restore steps are in MIGRATE.md under "Upgrading to 3.4.0". Log: `~/Library/Logs/tigerduck-backup.log`.
