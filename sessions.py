"""Shared Codex session manager.

Per-user HOME/CODEX_HOME isolation lives under /opt/data/users/<user>/.
Session metadata is persisted in /opt/data/users/<user>/sessions/<sid>.json so
the orchestrator can survive restarts. Codex itself persists conversation data
under CODEX_HOME; after the first successful run we capture the Codex session id
from JSONL events and use `codex exec resume` for later prompts.
"""
import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

USERS_ROOT = os.environ.get("USERS_ROOT", "/opt/data/users")
USER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
DEFAULT_MODEL = os.environ.get("CODEX_DEFAULT_MODEL", "").strip() or None
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_SANDBOX = os.environ.get("CODEX_SANDBOX", "workspace-write")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    id: str
    user: str
    cwd: str = ""
    model: str | None = None
    permission_mode: str = "workspace-write"
    system_prompt: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    max_turns: int | None = None
    codex_session_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: str = ""
    last_used_at: str = ""


SESSIONS: dict[str, Session] = {}


def slugify_user(raw: str) -> str:
    if not raw:
        raise ValueError("user is required")
    s = raw.strip().lower().replace(" ", "-")
    if not USER_SLUG_RE.match(s):
        raise ValueError(
            f"invalid user '{raw}': must be lowercase alphanumeric + '-' / '_', "
            "starting with alphanumeric, max 63 chars"
        )
    return s


def user_home(user: str) -> str:
    return os.path.join(USERS_ROOT, user)


def codex_home(user: str) -> str:
    return os.path.join(user_home(user), ".codex")


def ensure_user_dirs(user: str) -> str:
    home = user_home(user)
    os.makedirs(codex_home(user), exist_ok=True)
    os.makedirs(os.path.join(home, "workspace"), exist_ok=True)
    os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
    os.chmod(home, 0o700)
    return home


def _meta_path(user: str, sid: str) -> Path:
    return Path(user_home(user)) / "sessions" / f"{sid}.json"


def _save_meta(sess: Session) -> None:
    p = _meta_path(sess.user, sess.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "id": sess.id,
                "user": sess.user,
                "cwd": sess.cwd,
                "model": sess.model,
                "permission_mode": sess.permission_mode,
                "system_prompt": sess.system_prompt,
                "allowed_tools": sess.allowed_tools,
                "max_turns": sess.max_turns,
                "codex_session_id": sess.codex_session_id,
                "created_at": sess.created_at,
                "last_used_at": sess.last_used_at,
            },
            indent=2,
        )
    )


def _delete_meta(user: str, sid: str) -> None:
    try:
        _meta_path(user, sid).unlink()
    except FileNotFoundError:
        pass


def load_persisted_sessions() -> None:
    root = Path(USERS_ROOT)
    if not root.exists():
        return
    for user_dir in root.iterdir():
        sessions_dir = user_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for f in sessions_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            sid = d.get("id")
            if not sid:
                continue
            SESSIONS[sid] = Session(
                id=sid,
                user=d["user"],
                cwd=d.get("cwd", ""),
                model=d.get("model"),
                permission_mode=d.get("permission_mode", DEFAULT_SANDBOX),
                system_prompt=d.get("system_prompt"),
                allowed_tools=d.get("allowed_tools") or [],
                max_turns=d.get("max_turns"),
                codex_session_id=d.get("codex_session_id"),
                created_at=d.get("created_at", ""),
                last_used_at=d.get("last_used_at", ""),
            )


def update_system_prompt(sid: str, system_prompt: str | None) -> None:
    sess = SESSIONS[sid]
    if sess.system_prompt == system_prompt:
        return
    sess.system_prompt = system_prompt
    _save_meta(sess)


async def create_session(
    *,
    user: str,
    cwd: str | None,
    system_prompt: str | None,
    permission_mode: str,
    allowed_tools: list[str],
    max_turns: int | None,
    model: str | None = None,
) -> str:
    user = slugify_user(user)
    home = ensure_user_dirs(user)
    effective_cwd = cwd or os.path.join(home, "workspace")
    os.makedirs(effective_cwd, exist_ok=True)
    effective_model = (model or DEFAULT_MODEL) or None

    sid = str(uuid.uuid4())
    now = _now()
    sess = Session(
        id=sid,
        user=user,
        cwd=effective_cwd,
        model=effective_model,
        permission_mode=permission_mode or DEFAULT_SANDBOX,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools or [],
        max_turns=max_turns,
        created_at=now,
        last_used_at=now,
    )
    SESSIONS[sid] = sess
    _save_meta(sess)
    return sid


async def kill_session(sid: str) -> None:
    sess = SESSIONS.pop(sid, None)
    if sess:
        _delete_meta(sess.user, sess.id)


async def detach_all_sessions() -> None:
    return None


def _is_ollama_cloud(model: str | None) -> bool:
    """Models with the `:cloud` suffix route through the in-pod Ollama daemon
    (sidecar at 127.0.0.1:11434), which proxies to Ollama Cloud."""
    return bool(model and model.endswith(":cloud"))


OLLAMA_LOCAL_URL = os.environ.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
OLLAMA_PROFILE_NAME = "ollama-launch"
OLLAMA_PROFILE_BLOCK = f"""
[profiles.{OLLAMA_PROFILE_NAME}]
openai_base_url = "{OLLAMA_LOCAL_URL}/v1/"
forced_login_method = "api"
model_provider = "{OLLAMA_PROFILE_NAME}"

[model_providers.{OLLAMA_PROFILE_NAME}]
name = "Ollama"
base_url = "{OLLAMA_LOCAL_URL}/v1/"
"""


def _ensure_ollama_profile(user: str) -> None:
    """Idempotently add the `ollama-launch` profile to the user's codex config."""
    cfg_path = Path(codex_home(user)) / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing = cfg_path.read_text() if cfg_path.exists() else ""
    if f"[profiles.{OLLAMA_PROFILE_NAME}]" in existing:
        return
    sep = "" if existing.endswith("\n") or not existing else "\n"
    cfg_path.write_text(existing + sep + OLLAMA_PROFILE_BLOCK)


def _env(sess: Session) -> dict[str, str]:
    env = os.environ.copy()
    home = user_home(sess.user)
    env.update(
        {
            "HOME": home,
            "USER": sess.user,
            "CODEX_HOME": codex_home(sess.user),
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "TMPDIR": os.path.join(user_home(sess.user), "tmp"),
        }
    )
    os.makedirs(env["TMPDIR"], exist_ok=True)
    if _is_ollama_cloud(sess.model):
        env["OPENAI_API_KEY"] = "ollama"
    return env


def _sandbox_args(permission_mode: str) -> list[str]:
    mode = (permission_mode or DEFAULT_SANDBOX).strip()
    if mode in ("bypassPermissions", "dangerously-bypass"):
        return ["--dangerously-bypass-approvals-and-sandbox"]
    if mode in ("read-only", "workspace-write", "danger-full-access"):
        return ["--sandbox", mode]
    return ["--sandbox", DEFAULT_SANDBOX]


def _base_args(sess: Session) -> list[str]:
    args = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check", "--cd", sess.cwd]
    if _is_ollama_cloud(sess.model):
        args.extend(["--profile", OLLAMA_PROFILE_NAME])
    args.extend(_sandbox_args(sess.permission_mode))
    if sess.model:
        args.extend(["--model", sess.model])
    return args


def _prompt(sess: Session, prompt: str) -> str:
    if not sess.system_prompt:
        return prompt
    return f"{sess.system_prompt}\n\nUser prompt:\n{prompt}"


def _extract_session_id(ev: dict) -> str | None:
    for key in ("thread_id", "threadId", "session_id", "sessionId", "conversation_id", "conversationId"):
        value = ev.get(key)
        if isinstance(value, str) and value:
            return value
    payload = ev.get("payload")
    if isinstance(payload, dict):
        return _extract_session_id(payload)
    return None


def _text_from_event(ev: dict) -> str | None:
    for key in ("delta", "text", "message", "content", "output_text"):
        value = ev.get(key)
        if isinstance(value, str) and value:
            return value
    item = ev.get("item")
    if isinstance(item, dict):
        return _text_from_event(item)
    return None


def _tool_from_event(ev: dict) -> tuple[str, object] | None:
    name = ev.get("name") or ev.get("tool_name")
    if not isinstance(name, str):
        item = ev.get("item")
        if isinstance(item, dict):
            return _tool_from_event(item)
        return None
    return name, ev.get("input") or ev.get("arguments") or {}


async def stream_query(sid: str, prompt: str) -> AsyncIterator[dict]:
    sess = SESSIONS.get(sid)
    if not sess:
        raise KeyError(f"no such session: {sid}")

    async with sess.lock:
        ensure_user_dirs(sess.user)
        if _is_ollama_cloud(sess.model):
            _ensure_ollama_profile(sess.user)
        sess.last_used_at = _now()
        _save_meta(sess)

        if sess.codex_session_id:
            args = _base_args(sess) + ["resume", sess.codex_session_id, "-"]
        else:
            args = _base_args(sess) + ["-"]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(sess),
            cwd=sess.cwd,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc.stdin.write(_prompt(sess, prompt).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        stderr_parts: list[str] = []

        async def drain_stderr() -> None:
            async for chunk in proc.stderr:
                stderr_parts.append(chunk.decode(errors="replace"))

        stderr_task = asyncio.create_task(drain_stderr())
        text_parts: list[str] = []
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "text", "content": line}
                    continue

                codex_sid = _extract_session_id(ev)
                if codex_sid and not sess.codex_session_id:
                    sess.codex_session_id = codex_sid
                    _save_meta(sess)

                ev_type = str(ev.get("type", ""))
                if "tool" in ev_type or "exec" in ev_type or "command" in ev_type:
                    tool = _tool_from_event(ev)
                    if tool:
                        name, tool_input = tool
                        yield {"type": "tool", "name": name, "input": tool_input}
                    continue

                text = _text_from_event(ev)
                if text and (
                    "delta" in ev_type
                    or "message" in ev_type
                    or "output" in ev_type
                    or ev_type == "item.completed"
                ):
                    text_parts.append(text)
                    yield {"type": "text", "content": text}
        finally:
            code = await proc.wait()
            await stderr_task

        if code != 0:
            stderr_text = "".join(stderr_parts).strip()
            stdout_tail = "".join(text_parts[-3:]).strip() if text_parts else ""
            parts = []
            if stdout_tail:
                parts.append(stdout_tail)
            if stderr_text:
                parts.append(stderr_text)
            detail = "\n".join(parts) or f"codex exited with status {code}"
            yield {"type": "text", "content": f"Codex failed: {detail}"}
        yield {"type": "done", "cost_usd": None, "turns": None}


async def oauth_warmer_loop(
    *,
    check_interval: int = 1800,
    refresh_threshold: int = 3600,
) -> None:
    """Force Codex CLI to refresh OAuth tokens before they expire.

    Codex auth.json carries `tokens.access_token` + `tokens.expires_at` (ISO8601).
    When expiry is within `refresh_threshold` seconds, fire a minimal one-shot
    to trigger the CLI's refresh path so an idle pod doesn't go stale.
    """
    import time
    while True:
        try:
            root = Path(USERS_ROOT)
            if root.exists():
                for user_dir in root.iterdir():
                    auth = user_dir / ".codex" / "auth.json"
                    if not auth.is_file():
                        continue
                    try:
                        d = json.loads(auth.read_text())
                        tokens = d.get("tokens") or {}
                        exp_raw = tokens.get("expires_at") or tokens.get("expiresAt")
                        if isinstance(exp_raw, str):
                            exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00")).timestamp()
                        elif isinstance(exp_raw, (int, float)):
                            exp = float(exp_raw) / (1000 if exp_raw > 1e12 else 1)
                        else:
                            continue
                    except Exception:
                        continue
                    remaining = exp - time.time()
                    if remaining > refresh_threshold:
                        continue
                    user = user_dir.name
                    print(f"[oauth-warmer] refreshing {user} (expires in {int(remaining)}s)")
                    try:
                        await run_one_shot(
                            user=user,
                            cwd=None,
                            system_prompt=None,
                            permission_mode="read-only",
                            allowed_tools=[],
                            max_turns=1,
                            prompt="ok",
                            model=None,
                        )
                    except Exception as e:
                        print(f"[oauth-warmer] {user} refresh failed: {e}")
        except Exception as e:
            print(f"[oauth-warmer] loop error: {e}")
        await asyncio.sleep(check_interval)


async def run_one_shot(
    *,
    user: str,
    cwd: str | None,
    system_prompt: str | None,
    permission_mode: str,
    allowed_tools: list[str],
    max_turns: int | None,
    prompt: str,
    model: str | None = None,
) -> dict:
    sid = await create_session(
        user=user,
        cwd=cwd,
        system_prompt=system_prompt,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        model=model,
    )
    try:
        text_parts: list[str] = []
        tools: list[str] = []
        async for ev in stream_query(sid, prompt):
            if ev["type"] == "text":
                text_parts.append(ev["content"])
            elif ev["type"] == "tool":
                tools.append(ev["name"])
        return {"text": "\n".join(text_parts), "tools": tools, "cost_usd": None}
    finally:
        await kill_session(sid)
