# Codex Orchestrator

FastAPI + web UI + Telegram wrapper for Codex CLI, ported from the local
`claude-orchestrator` project.

## Run Locally

```bash
cd codex-orchestrator
cp .env.example .env
# Edit .env and replace every placeholder secret.
set -a
. ./.env
set +a
python -m uvicorn server:app --host 0.0.0.0 --port 8765
```

Open `http://localhost:8765/`, log in.

## First Admin User

On startup, if the web-user database is empty, the server bootstraps the first
admin user from environment variables:

```bash
export WEB_AUTH_USERNAME=admin
export WEB_AUTH_PASSWORD='<at least 12 chars>'
```

Use `WEB_AUTH_PASSWORD_HASH` instead of `WEB_AUTH_PASSWORD` if you want to pass
a precomputed password hash. Set `WEB_AUTH_TOTP_SECRET` to require 2FA for the
bootstrap admin from the first login.

After logging in as the admin user, open **Users** in the web UI to create more
users, reset passwords, enable or reset 2FA, grant admin access, or disable
accounts. Each authenticated username gets its own isolated workspace under
`/opt/data/users/<user>/`.

After login, open **Passkeys** to enroll a passkey for the current user.
For production passkeys behind a real domain, set `WEB_AUTH_ORIGIN` and
`WEB_AUTH_RP_ID` to match the public HTTPS origin and relying-party ID.

## Telegram and Scheduled Jobs

Telegram support is optional. Set `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ALLOWED_CHAT_ID` to enable it. Users can manage schedules with
`/schedule`, `/jobs`, and `/unschedule`, and agent sessions can create schedules
directly through the bundled `orchestrator-jobs` CLI instead of asking the human
to type a command.

Run the tick endpoint once per minute from cron, Kubernetes CronJob, or another
scheduler:

```bash
curl -fsS -X POST -H "X-API-Key: $ORCHESTRATOR_API_KEY" \
  http://localhost:8765/jobs/tick
```

## Codex Requirements

- `codex` must be on `PATH`, or set `CODEX_BIN=/path/to/codex`.
- Each orchestrator user gets isolated `HOME` and `CODEX_HOME` under
  `/opt/data/users/<user>/`.
- Seed auth/config into `/opt/data/users/<user>/.codex/` before using the
  service in a container or Kubernetes.
- Default model can be set with `CODEX_DEFAULT_MODEL`.
- Default sandbox can be set with `CODEX_SANDBOX=workspace-write`.

## OAuth Token Refresh

Codex ChatGPT login tokens (`/opt/data/users/<user>/.codex/auth.json`) auto-
refresh when the CLI runs, but an **idle pod** never triggers the refresh and
tokens expire. A background warmer in `sessions.oauth_warmer_loop` checks each
user's `auth.json` every 30 minutes and fires a no-op session when
`expires_at` is within 1 hour.

To bootstrap a fresh pod, seed creds from your host:

```bash
kubectl -n codex-orchestrator cp ~/.codex/auth.json \
  codex-orchestrator/$(kubectl -n codex-orchestrator get pod \
    -l app.kubernetes.io/name=codex-orchestrator \
    -o jsonpath='{.items[0].metadata.name}'):/opt/data/users/<user>/.codex/auth.json
```

## Ollama Cloud Models

Models with the `:cloud` suffix (`glm-5.1:cloud`, `deepseek-v4-flash:cloud`,
etc.) route through an in-pod Ollama daemon sidecar, which proxies to Ollama
Cloud via your signed-in account. The sidecar listens on `127.0.0.1:11434`.

When a `:cloud` model is selected, the orchestrator writes an `ollama-launch`
profile to `~/.codex/config.toml` and passes `--profile ollama-launch
-m <model>` to the codex CLI (env `OPENAI_API_KEY=ollama`).

### Sign in once per pod

The ed25519 signing key at `/root/.ollama/id_ed25519` (PVC subPath
`ollama-state`) survives pod restarts, so this is a one-time step per
orchestrator deployment:

```bash
kubectl -n codex-orchestrator exec -it -c ollama \
  deploy/codex-orchestrator -- ollama signin
```

The command prints a `https://ollama.com/connect?...` URL. Open it in a
browser, approve, and the command unblocks.

### Verify

```bash
kubectl -n codex-orchestrator exec deploy/codex-orchestrator -c orchestrator -- \
  curl -sS http://127.0.0.1:11434/v1/chat/completions \
  -H 'content-type: application/json' -H 'authorization: Bearer ollama' \
  -d '{"model":"glm-5.1:cloud","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'
```

A 200 with `"role":"assistant"` content confirms the daemon → Ollama Cloud
path is wired. Then in Telegram:

```
/model glm-5.1:cloud
/new
<prompt>
```

## Notes

This uses `codex exec --json` and `codex exec resume` under the hood. It does
not use a Python SDK client because the local Codex installation exposes a CLI
interface for this workflow.
