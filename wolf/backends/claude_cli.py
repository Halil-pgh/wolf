"""Claude backend: every agent turn is one headless `claude -p` call on the user's Claude Code login.

No API key is needed; the calls count against the user's Claude plan limits. Each call is stateless:

    claude -p --model sonnet --system-prompt <system> --tools "" --output-format json \
        --json-schema <schema> --no-session-persistence --strict-mcp-config [--effort <level>]

The prompt is sent on stdin (`--tools` is variadic and would swallow a positional prompt), and the
process runs in a private empty directory, so no project CLAUDE.md or other context leaks in.
Never add `--bare`: it turns off the OAuth/subscription login.
"""

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import weakref
from datetime import datetime

from .base import BackendError

EXCERPT_CHARS = 300

_AUTH = re.compile(
    r"not logged in|log ?in required|please (run )?/?log ?in|/login|authenticat|unauthori[sz]ed"
    r"|invalid api key|oauth token|\b401\b",
    re.I,
)
_OVERLOADED = re.compile(r"overloaded|\b529\b", re.I)
_LIMIT = re.compile(
    r"usage limit|rate[ _-]?limit|limit reached|hit your limit|too many requests|quota|\b429\b",
    re.I,
)
# Older CLIs report a subscription limit as "Claude AI usage limit reached|<unix time of reset>".
_RESET_EPOCH = re.compile(r"limit reached\|(\d{10})")


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _diagnose(text: str) -> tuple[str | None, bool]:
    """A plain-language explanation for well-known failures (login, limits) or None, and whether
    the failure is fatal (retrying can't help)."""
    if _AUTH.search(text):
        return "Claude is not logged in (or the login expired): run `claude` and use /login", True
    if _OVERLOADED.search(text):
        return "Claude is overloaded or rate-limited right now; wait a moment and try again", False
    if _LIMIT.search(text):
        hint = "Claude usage limit reached; wait for your plan's limit to reset"
        if m := _RESET_EPOCH.search(text):
            hint += f" (resets around {datetime.fromtimestamp(int(m.group(1))):%Y-%m-%d %H:%M})"
        return hint, True
    return None, False


def _failure(what: str, detail: str) -> BackendError:
    msg = f"{what}: {_excerpt(detail)}" if detail.strip() else what
    hint, fatal = _diagnose(f"{what} {detail}")
    return BackendError(f"{hint} [{msg}]" if hint else msg, fatal=fatal)


def _parse_envelope(stdout: str) -> dict | None:
    """The CLI's result object from stdout, or None if stdout holds no JSON object."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
        for line in reversed(text.splitlines()):  # tolerate stray log lines before the JSON
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    obj = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    if isinstance(obj, list):  # verbose mode prints every message; the result comes last
        results = [m for m in obj if isinstance(m, dict) and m.get("type") == "result"]
        obj = results[-1] if results else None
    return obj if isinstance(obj, dict) else None


def _json_object(text: str) -> dict | None:
    """Parse a JSON object from model text, tolerating code fences or prose around the {...}."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _extract_reply(envelope: dict) -> dict | None:
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    if isinstance(structured, str) and (obj := _json_object(structured)):
        return obj
    result = envelope.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return _json_object(result)
    return None


def _error_detail(envelope: dict) -> str:
    parts = []
    if isinstance(envelope.get("result"), str):
        parts.append(envelope["result"])
    errors = envelope.get("errors")
    if isinstance(errors, list):
        parts.extend(str(e) for e in errors)
    elif errors:
        parts.append(str(errors))
    return " | ".join(p for p in parts if p.strip())


class ClaudeCLIBackend:
    def __init__(
        self,
        model: str = "sonnet",
        effort: str | None = None,
        timeout: float = 180.0,
        concurrency: int = 4,
        claude_bin: str = "claude",
        extra_args: list[str] | None = None,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        path = shutil.which(claude_bin)
        if path is None:
            raise BackendError(
                f"Can't find the Claude Code CLI ({claude_bin!r}). Install Claude Code and log in by "
                "running `claude`, or pass claude_bin with the full path to the executable. "
                "To try the game without AI, use --backend mock."
            )
        self.claude_bin = path
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.extra_args = list(extra_args or [])
        self.max_concurrency = concurrency
        self._sem = threading.Semaphore(concurrency)
        # A private empty working directory, so no project CLAUDE.md or files reach the agents.
        self.workdir = tempfile.mkdtemp(prefix="wolf-agents-")
        weakref.finalize(self, shutil.rmtree, self.workdir, ignore_errors=True)
        self._lock = threading.Lock()
        self._stats = {
            "calls": 0,
            "errors": 0,
            "seconds": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }

    def build_command(self, system: str, schema: dict) -> list[str]:
        cmd = [
            self.claude_bin, "-p",
            "--model", self.model,
            "--system-prompt", system,
            "--tools", "",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--no-session-persistence",
            "--strict-mcp-config",
        ]  # fmt: skip
        if self.effort:
            cmd += ["--effort", self.effort]
        return cmd + self.extra_args

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        if not prompt.strip():
            raise BackendError("refusing to call claude with an empty prompt")
        cmd = self.build_command(system, schema)
        with self._sem:
            start = time.monotonic()
            envelope: dict | None = None
            ok = False
            try:
                proc = self._run(cmd, prompt)
                envelope = _parse_envelope(proc.stdout)
                reply = self._interpret(proc, envelope)
                ok = True
                return reply
            finally:
                self._account(time.monotonic() - start, envelope, ok)

    def _run(self, cmd: list[str], prompt: str) -> subprocess.CompletedProcess:
        try:
            # subprocess.run kills the child if the timeout expires (or on Ctrl+C) before re-raising.
            return subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.workdir,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(f"claude timed out after {self.timeout:g}s (the process was killed)") from None
        except OSError as e:
            raise BackendError(f"could not run {self.claude_bin}: {e}") from None

    def _interpret(self, proc: subprocess.CompletedProcess, envelope: dict | None) -> dict:
        if proc.returncode != 0:
            if envelope is not None:
                detail = _error_detail(envelope)
                status = envelope.get("api_error_status")
                what = f"claude exited with code {proc.returncode}" + (
                    f" (API error {status})" if status else ""
                )
            else:
                detail = proc.stderr.strip() or proc.stdout.strip()
                what = f"claude exited with code {proc.returncode}"
            raise _failure(what, detail)
        if envelope is None:
            out = proc.stdout.strip()
            raise _failure(
                "claude printed output that is not valid JSON" if out else "claude printed nothing",
                out or proc.stderr,
            )
        subtype = envelope.get("subtype", "success")
        if envelope.get("is_error") or subtype != "success":
            status = envelope.get("api_error_status")
            what = f"claude reported an error ({subtype}" + (f", API error {status}" if status else "") + ")"
            raise _failure(what, _error_detail(envelope))
        reply = _extract_reply(envelope)
        if reply is None:
            raise _failure("claude's reply has no JSON object", str(envelope.get("result") or ""))
        return reply

    def _account(self, seconds: float, envelope: dict | None, ok: bool) -> None:
        usage = (envelope or {}).get("usage") or {}

        def num(d: dict, key: str) -> float:
            v = d.get(key)
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

        with self._lock:
            s = self._stats
            s["calls"] += 1
            s["errors"] += 0 if ok else 1
            s["seconds"] += seconds
            if isinstance(usage, dict):
                s["input_tokens"] += int(
                    num(usage, "input_tokens")
                    + num(usage, "cache_read_input_tokens")
                    + num(usage, "cache_creation_input_tokens")
                )
                s["output_tokens"] += int(num(usage, "output_tokens"))
            s["cost_usd"] += num(envelope or {}, "total_cost_usd")

    def stats(self) -> dict:
        with self._lock:
            s = dict(self._stats)
        s["seconds"] = round(s["seconds"], 3)
        s["cost_usd"] = round(s["cost_usd"], 6)
        s["model"] = self.model
        return s
