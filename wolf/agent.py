"""One AI player: builds its requests, calls the backend, validates the reply, retries, and falls back."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from . import prompts
from .backends.base import BackendError
from .state import Decision, Player, Request, Task

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Game

NOTES_CLIP_WORDS = 150  # the agent is asked for < 100 words; anything past this is cut
CLIP_FACTOR = 1.5  # speech and reasons may run this far over their word limit before being cut
_QUOTES = {'"': '"', "“": "”", "”": "”", "'": "'", "‘": "’", "«": "»", "„": "“"}


class Agent:
    def __init__(self, player: Player, game: "Game"):
        self.player = player
        self.game = game
        # Used only for fallbacks. Seeded from the game rng so mock games stay reproducible.
        self.rng = random.Random(game.rng.getrandbits(64))
        self._system: str | None = None

    @property
    def name(self) -> str:
        return self.player.name

    @property
    def system(self) -> str:
        if self._system is None:
            self._system = prompts.system_prompt(self.game, self.player)
        return self._system

    def build(self, task: Task) -> Request:
        """MAIN THREAD ONLY: reads game state."""
        req = Request(
            system=self.system,
            prompt=prompts.turn_prompt(self.game, self.player, task),
            schema=prompts.schema_for(task),
        )
        self.game.requests.append((self.player.name, task.kind, req))
        return req

    def execute(self, task: Task, req: Request) -> Decision:
        """Thread-safe: only calls the backend; never touches game state."""
        if task.targets is not None and len(task.targets) == 1 and task.speech_words is None:
            return Decision(target=task.targets[0], reason="(the only choice)", auto=True)

        backend = self.game.backend
        attempts = 1 + max(0, self.game.config.retries)
        prompt = req.prompt
        error = ""
        backend_errors = 0
        for _ in range(attempts):
            try:
                reply = backend.complete(req.system, prompt, req.schema)
            except BackendError as exc:
                if exc.fatal:
                    raise
                backend_errors += 1
                error = f"backend error: {exc}"
                continue
            try:
                return parse_reply(task, reply, speaker=self.player.name)
            except ValueError as exc:
                error = f"invalid reply: {exc}"
                prompt = (
                    f"{req.prompt}\n\nIMPORTANT: your previous reply was rejected ({exc}). "
                    "Follow the required format exactly."
                )
        return self._fallback(task, error, backend_error=backend_errors == attempts)

    def _fallback(self, task: Task, error: str, backend_error: bool) -> Decision:
        target = self.rng.choice(task.targets) if task.targets else None
        return Decision(target=target, fallback=True, backend_error=backend_error, error=error)


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------

def parse_reply(task: Task, reply: object, speaker: str | None = None) -> Decision:
    """Turn a backend reply into a Decision, or raise ValueError saying what is wrong."""
    if not isinstance(reply, dict):
        raise ValueError("the reply must be a JSON object")
    dec = Decision(thought=_text_field(reply, "thought"), notes=clip_words(_text_field(reply, "notes"), NOTES_CLIP_WORDS))

    if task.urge:
        dec.urge = _urge(reply.get("urge"))
        dec.ready_to_vote = _flag(reply.get("ready_to_vote"))

    if task.speech_words is not None:
        speech = reply.get("speech")
        if not isinstance(speech, str):
            raise ValueError('"speech" is missing')
        speech = clean_speech(speech, speaker)
        if not speech:
            raise ValueError('"speech" must not be empty this time')
        dec.speech = clip_words(speech, int(task.speech_words * CLIP_FACTOR))

    if task.asks is not None and dec.speech:
        dec.asks = _names(reply.get("asks"), task.asks)

    if task.targets is not None:
        target = reply.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError('"target" is missing')
        canonical = {t.casefold(): t for t in task.targets}
        key = _unquote(target.strip()).casefold()
        if key not in canonical:
            raise ValueError(f'"target" must be one of {", ".join(task.targets)}, not {target!r}')
        dec.target = canonical[key]

    if task.reason_words is not None:
        reason = reply.get("reason")
        if not isinstance(reason, str) or not clean_speech(reason):
            raise ValueError('"reason" is missing')
        dec.reason = clip_words(clean_speech(reason), int(task.reason_words * CLIP_FACTOR))

    return dec


def _urge(value: object) -> int:
    """0-10, rounded half up and clamped. Accepts numbers and numeric strings."""
    if isinstance(value, str):
        try:
            value = float(value.strip().removesuffix("/10"))
        except ValueError:
            value = None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('"urge" must be a whole number from 0 to 10')
    return min(10, max(0, math.floor(value + 0.5)))


def _flag(value: object) -> bool | None:
    """A JSON boolean (or "true"/"false"); None when missing, so the player's readiness doesn't change."""
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if value is None or isinstance(value, bool):
        return value
    raise ValueError('"ready_to_vote" must be true or false')


def _names(value: object, legal: list[str]) -> list[str]:
    """The legal names in `value`, canonical, in order, without repeats. Unknown names are dropped."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError('"asks" must be a list of names')
    canonical = {t.casefold(): t for t in legal}
    names: list[str] = []
    for item in value:
        name = canonical.get(_unquote(item).casefold()) if isinstance(item, str) else None
        if name and name not in names:
            names.append(name)
    return names


def _text_field(reply: dict, key: str) -> str:
    value = reply.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f'"{key}" must be a string')
    return value.strip()


def _unquote(text: str) -> str:
    """Strip matching wrapping quotes (repeatedly) and surrounding whitespace."""
    text = text.strip()
    while len(text) >= 2 and _QUOTES.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def clean_speech(text: str, speaker: str | None = None) -> str:
    text = _unquote(text)
    if speaker:
        for prefix in (f"{speaker}:", f"{speaker} :"):
            if text.startswith(prefix):
                text = _unquote(text[len(prefix):])
                break
    return text


def clip_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(",;:-–—") + "…"
