"""ClaudeCLIBackend tests. They use a FAKE `claude` executable only: no network, no model calls."""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from wolf.backends.base import BackendError
from wolf.backends.claude_cli import ClaudeCLIBackend

PROJECT_DIR = Path(__file__).resolve().parent.parent
SYSTEM = "You are Alice, a villager. Stay in character."
PROMPT = "PROMPT-MARKER-7f3a: Day 1. Who do you vote for?"
SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "target": {"type": "string", "enum": ["Bob", "Clara"]},
        "speech": {"type": "string"},
    },
    "required": ["thought", "target", "speech"],
}
REPLY = {"thought": "Bob is dodging.", "target": "Bob", "speech": "I vote Bob."}

# The fake reads fake_config.json from its own directory, records each call to calls/<pid>-<ns>.json,
# optionally sleeps, then prints the configured stdout/stderr and exits with the configured code.
FAKE_CLAUDE = """\
#!{python}
import json, os, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
cfg = json.loads((here / "fake_config.json").read_text())
rec = {{
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "cwd": os.getcwd(),
    "cwd_files": os.listdir("."),
    "pid": os.getpid(),
    "start": time.time(),
}}
calls = here / "calls"
calls.mkdir(exist_ok=True)
out = calls / f"{{os.getpid()}}-{{time.time_ns()}}.json"
out.write_text(json.dumps(rec))
time.sleep(cfg.get("sleep", 0))
rec["end"] = time.time()
out.write_text(json.dumps(rec))
if cfg.get("stderr"):
    sys.stderr.write(cfg["stderr"])
if "stdout" in cfg:
    sys.stdout.write(cfg["stdout"])
else:
    sys.stdout.write(json.dumps(cfg["envelope"]))
sys.exit(cfg.get("exit", 0))
"""


def envelope(**overrides) -> dict:
    env = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 4356,
        "num_turns": 2,
        "result": json.dumps(REPLY),
        "structured_output": REPLY,
        "total_cost_usd": 0.002752,
        "usage": {
            "input_tokens": 1212,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 308,
        },
        "session_id": "fake-session",
        "stop_reason": "tool_use",
    }
    env.update(overrides)
    return env


class Fake:
    def __init__(self, directory: Path):
        self.dir = directory
        self.bin = directory / "claude"
        self.bin.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.bin.chmod(0o755)
        self.configure(envelope=envelope())

    def configure(self, **cfg) -> None:
        (self.dir / "fake_config.json").write_text(json.dumps(cfg))

    def calls(self) -> list[dict]:
        folder = self.dir / "calls"
        if not folder.exists():
            return []
        return sorted((json.loads(p.read_text()) for p in folder.iterdir()), key=lambda r: r["start"])

    def backend(self, **kw) -> ClaudeCLIBackend:
        return ClaudeCLIBackend(claude_bin=str(self.bin), **kw)


@pytest.fixture
def fake(tmp_path) -> Fake:
    return Fake(tmp_path)


def call(backend: ClaudeCLIBackend) -> dict:
    return backend.complete(SYSTEM, PROMPT, SCHEMA)


def after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# --- success and parsing ---------------------------------------------------------------------------


def test_success_uses_structured_output(fake):
    fake.configure(envelope=envelope(result="ignored prose", structured_output=REPLY))
    assert call(fake.backend()) == REPLY


@pytest.mark.parametrize(
    "result",
    [
        json.dumps(REPLY),
        "Here is my decision:\n```json\n" + json.dumps(REPLY, indent=2) + "\n```\nGood luck!",
        "Sure. " + json.dumps(REPLY) + " That's all.",
    ],
)
def test_falls_back_to_result_string(fake, result):
    env = envelope(result=result)
    del env["structured_output"]
    fake.configure(envelope=env)
    assert call(fake.backend()) == REPLY


def test_non_dict_structured_output_falls_back_to_result(fake):
    fake.configure(envelope=envelope(structured_output=None, result="Answer: " + json.dumps(REPLY)))
    assert call(fake.backend()) == REPLY


def test_reply_without_json_object_raises(fake):
    fake.configure(envelope=envelope(structured_output=None, result="I would rather not say."))
    with pytest.raises(BackendError, match="no JSON object"):
        call(fake.backend())


# --- errors ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_code, env",
    [
        (1, envelope(is_error=True, structured_output=None, result="Claude AI usage limit reached|1759300000")),
        (0, envelope(is_error=True, structured_output=None, result="You've hit your limit · resets 7pm")),
        (
            1,
            envelope(
                is_error=True,
                api_error_status=429,
                structured_output=None,
                result='API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
            ),
        ),
    ],
)
def test_rate_limit_error_mentions_the_limit(fake, exit_code, env):
    fake.configure(envelope=env, exit=exit_code)
    with pytest.raises(BackendError) as exc:
        call(fake.backend())
    msg = str(exc.value)
    assert "limit" in msg.lower()
    assert "Claude usage limit reached" in msg
    assert exc.value.fatal  # retrying can't help, so the game should stop at once


def test_overloaded_error_says_so(fake):
    fake.configure(
        envelope=envelope(is_error=True, api_error_status=529, structured_output=None, result="API Error: Overloaded"),
        exit=1,
    )
    with pytest.raises(BackendError, match="overloaded or rate-limited") as exc:
        call(fake.backend())
    assert not exc.value.fatal  # transient: worth retrying


@pytest.mark.parametrize(
    "cfg",
    [
        {"envelope": envelope(is_error=True, structured_output=None, result="Invalid API key · Please run /login"), "exit": 1},
        {"stdout": "", "stderr": "Error: Not logged in. Please run claude and log in.", "exit": 1},
        {
            "envelope": envelope(
                is_error=True, api_error_status=401, structured_output=None, result="OAuth token has expired"
            ),
            "exit": 0,
        },
    ],
)
def test_auth_error_gives_login_hint(fake, cfg):
    fake.configure(**cfg)
    with pytest.raises(BackendError) as exc:
        call(fake.backend())
    msg = str(exc.value)
    assert "not logged in" in msg
    assert "/login" in msg
    assert exc.value.fatal


def test_non_zero_exit_includes_short_excerpt(fake):
    fake.configure(stdout="", stderr="boom: something broke " + "x" * 2000, exit=2)
    with pytest.raises(BackendError) as exc:
        call(fake.backend())
    msg = str(exc.value)
    assert "code 2" in msg
    assert "boom: something broke" in msg
    assert len(msg) < 400


def test_error_subtype_raises(fake):
    fake.configure(envelope=envelope(subtype="error_max_turns", structured_output=None, result=""))
    with pytest.raises(BackendError, match="error_max_turns"):
        call(fake.backend())


def test_timeout_kills_the_process(fake):
    fake.configure(envelope=envelope(), sleep=5)
    backend = fake.backend(timeout=0.5)
    start = time.monotonic()
    with pytest.raises(BackendError, match="timed out"):
        call(backend)
    assert time.monotonic() - start < 4
    [rec] = fake.calls()
    assert "end" not in rec  # it never finished its sleep
    with pytest.raises(ProcessLookupError):
        os.kill(rec["pid"], 0)
    assert backend.stats()["errors"] == 1


@pytest.mark.parametrize("stdout, match", [("this is not json {", "not valid JSON"), ("", "printed nothing")])
def test_invalid_json_raises(fake, stdout, match):
    fake.configure(stdout=stdout)
    with pytest.raises(BackendError, match=match):
        call(fake.backend())


def test_missing_binary_raises():
    with pytest.raises(BackendError, match="Install Claude Code"):
        ClaudeCLIBackend(claude_bin="claude-definitely-not-installed-9d1e")


def test_empty_prompt_is_rejected_without_a_call(fake):
    with pytest.raises(BackendError):
        fake.backend().complete(SYSTEM, "   ", SCHEMA)
    assert fake.calls() == []


# --- command line, stdin and working directory ------------------------------------------------------


def test_argv_and_stdin(fake):
    call(fake.backend(model="haiku"))
    [rec] = fake.calls()
    argv = rec["argv"]
    assert "-p" in argv
    assert after(argv, "--tools") == ""
    assert json.loads(after(argv, "--json-schema")) == SCHEMA
    assert after(argv, "--model") == "haiku"
    assert after(argv, "--system-prompt") == SYSTEM
    assert after(argv, "--output-format") == "json"
    assert "--no-session-persistence" in argv
    assert "--strict-mcp-config" in argv
    assert "--effort" not in argv
    assert "--bare" not in argv
    assert rec["stdin"] == PROMPT
    assert not any("PROMPT-MARKER" in a for a in argv)


def test_effort_only_when_set(fake):
    call(fake.backend(effort="low", extra_args=["--disable-slash-commands"]))
    [rec] = fake.calls()
    argv = rec["argv"]
    assert after(argv, "--effort") == "low"
    assert argv[-1] == "--disable-slash-commands"
    assert "--bare" not in argv


def test_exact_command(fake):
    backend = fake.backend(model="sonnet")
    assert backend.build_command(SYSTEM, {"type": "object"}) == [
        str(fake.bin), "-p",
        "--model", "sonnet",
        "--system-prompt", SYSTEM,
        "--tools", "",
        "--output-format", "json",
        "--json-schema", '{"type":"object"}',
        "--no-session-persistence",
        "--strict-mcp-config",
    ]  # fmt: skip


def test_runs_in_private_empty_directory(fake):
    backend = fake.backend()
    call(backend)
    [rec] = fake.calls()
    cwd = Path(rec["cwd"]).resolve()
    assert cwd == Path(backend.workdir).resolve()
    assert cwd != PROJECT_DIR and PROJECT_DIR not in cwd.parents
    assert cwd != Path.cwd().resolve()
    assert cwd.name.startswith("wolf-agents-")
    assert rec["cwd_files"] == []


# --- stats and concurrency --------------------------------------------------------------------------


def test_stats_accumulate(fake):
    usage = {"input_tokens": 100, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 20}
    fake.configure(envelope=envelope(usage=usage, total_cost_usd=0.01))
    backend = fake.backend(model="haiku")
    call(backend)
    call(backend)
    s = backend.stats()
    assert s["calls"] == 2
    assert s["errors"] == 0
    assert s["input_tokens"] == 230
    assert s["output_tokens"] == 40
    assert s["cost_usd"] == pytest.approx(0.02)
    assert s["seconds"] > 0
    assert s["model"] == "haiku"

    fake.configure(stdout="garbage", exit=1)
    with pytest.raises(BackendError):
        call(backend)
    s = backend.stats()
    assert s["calls"] == 3
    assert s["errors"] == 1
    assert s["input_tokens"] == 230


def test_concurrency_cap(fake):
    fake.configure(envelope=envelope(), sleep=0.3)
    backend = fake.backend(concurrency=2)
    assert backend.max_concurrency == 2
    results, errors = [], []

    def worker():
        try:
            results.append(call(backend))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert results == [REPLY] * 6

    calls = fake.calls()
    assert len(calls) == 6
    peak = max(sum(1 for o in calls if o["start"] <= c["start"] < o["end"]) for c in calls)
    assert peak <= 2
    assert peak == 2  # the two slots really were used in parallel
    assert backend.stats()["calls"] == 6
