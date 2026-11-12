"""The Claude Code hook against a live server: context injection, capture, and failing open."""

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from rufur.app import build_app

HOOK = Path(__file__).resolve().parents[1] / "integrations/claude-code/hooks/rufur_hook.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(cfg, service):
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(build_app(cfg, service), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def run_hook(payload: dict, url: str, home: Path) -> tuple[int, dict | None]:
    env = {"RUFUR_URL": url, "RUFUR_HOME": str(home), "RUFUR_AGENT": "claude-code", "PATH": "/usr/bin:/bin"}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=20)  # fmt: skip
    return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None


def test_session_start_and_prompt_inject_memories(server, service, tmp_path):
    service.remember("ui", "The user prefers tabs over spaces", subject="user", predicate="indentation", value="tabs",
                     source="user")  # fmt: skip
    service.remember("ui", "The staging server is staging.rufur.dev", subject="staging server", predicate="hostname",
                     value="staging.rufur.dev", source="user")  # fmt: skip

    code, out = run_hook({"hook_event_name": "SessionStart", "session_id": "cc1", "cwd": "/x/rufur", "source": "startup"},
                         server, tmp_path)  # fmt: skip
    assert code == 0
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "RUFUR memory is active" in ctx

    code, out = run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "cc1",
                          "prompt": "what is the staging server hostname?"}, server, tmp_path)  # fmt: skip
    assert code == 0
    if out is not None:  # only injected if not already sent at session start
        assert "staging.rufur.dev" in out["hookSpecificOutput"]["additionalContext"]
    else:
        assert "staging.rufur.dev" in ctx
    # The prompt itself was captured as a user event.
    assert service.db.scalar("SELECT count(*) FROM events WHERE role = 'user' AND content LIKE '%staging server hostname%'") == 1

    code, out = run_hook({"hook_event_name": "SessionEnd", "session_id": "cc1"}, server, tmp_path)
    assert code == 0 and out is None
    assert json.loads((tmp_path / "hook_sessions.json").read_text()) == {}


def test_hook_fails_open_when_rufur_is_down(tmp_path):
    for event in ("SessionStart", "UserPromptSubmit", "SessionEnd"):
        code, out = run_hook({"hook_event_name": event, "session_id": "x", "prompt": "hi"},
                             f"http://127.0.0.1:{free_port()}", tmp_path)  # fmt: skip
        assert code == 0 and out is None
