# Codex Orchestrator

FastAPI + web UI + Telegram wrapper for Codex CLI, ported from the local
`claude-orchestrator` project.

## Run Locally

```bash
cd codex-orchestrator
export ORCHESTRATOR_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
python -m uvicorn server:app --host 0.0.0.0 --port 8765
```

Then open `http://localhost:8765/`, set the API key, and set a user slug.

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
