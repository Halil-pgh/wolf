"""MockBackend: instant, seeded, no AI. Fills any schema with random legal choices and canned lines."""

from __future__ import annotations

import random
import re
import threading
import time

from .base import BackendError

_ALIVE_RE = re.compile(r"^Alive \(\d+\): (.+)$", re.MULTILINE)
_ARCHETYPE_RE = re.compile(r"^# Your personality\n(.+?):", re.MULTILINE)
_ROUND_RE = re.compile(r"discussion round (\d+)")

_SPEECH = [
    "I've been watching {name} closely, and something about their story doesn't add up.",
    "{name}, where were you when the screaming started last night?",
    "We can't keep guessing. I say we look hard at {name} today.",
    "I trust {name}, for now. Let's not tear each other apart.",
    "Quiet folks worry me more than loud ones. {name}, speak up.",
    "I'm a simple soul. I'll follow the evidence wherever it leads.",
    "If I were a wolf, would I be this nervous? Think about it.",
    "Somebody here is lying, and I intend to find out who.",
    "{name} has been far too eager to point fingers.",
    "Let's look at who voted with whom yesterday before we decide anything.",
    "I have a bad feeling about {name}, but I can't prove it yet.",
    "Honestly, {name} is the only one making sense to me.",
]
_WOLF_CHAT = [
    "{target} is getting too close. Let's take them tonight.",
    "I say {target}. They've been sniffing around.",
    "Go for {target}; nobody will suspect us.",
    "{target} talks too much. Silence them.",
    "Take {target}. Tomorrow we lean on whoever defends them.",
]
_REASONS = [
    "Their story doesn't add up.",
    "Too quiet for my liking.",
    "They pushed hard on an innocent.",
    "Gut feeling, and I trust my gut.",
    "They dodged every question.",
    "They defended the wrong people.",
]
_THOUGHTS = [
    "{name} has been oddly quiet. Worth watching.",
    "I can't read {name} yet.",
    "{name}'s story holds up so far.",
    "If I push on {name}, who jumps to defend them?",
    "The votes around {name} look coordinated.",
]
_NOTES = [
    "Suspect {name}. Watch who defends them.",
    "No firm reads yet; {name} is a maybe.",
    "Leaning trust on {name}. Track the votes.",
]


class MockBackend:
    max_concurrency = 1

    def __init__(self, seed: int | None = None, error_rate: float = 0.0):
        self.rng = random.Random(seed)
        self.error_rate = error_rate
        self.calls = 0
        self.errors = 0
        self.seconds = 0.0
        self._lock = threading.Lock()

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        with self._lock:
            start = time.perf_counter()
            try:
                return self._complete(system, prompt, schema)
            finally:
                self.seconds += time.perf_counter() - start

    def stats(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "seconds": round(self.seconds, 3)}

    def _complete(self, system: str, prompt: str, schema: dict) -> dict:
        rng = self.rng
        self.calls += 1
        props: dict = schema.get("properties", {})
        others = self._others(prompt)
        name = rng.choice(others) if others else "everyone"

        target = rng.choice(props["target"]["enum"]) if "target" in props else None
        reply: dict = {}
        for key, spec in props.items():
            if key == "target":
                reply[key] = target
            elif key == "urge":
                reply[key] = self._urge(system, prompt)
            elif key == "speech":
                if target is not None:
                    reply[key] = rng.choice(_WOLF_CHAT).format(target=target)
                elif reply.get("urge") == 0 and rng.random() < 0.5:
                    reply[key] = ""
                else:
                    reply[key] = rng.choice(_SPEECH).format(name=name)
            elif key == "asks":
                legal = spec.get("items", {}).get("enum", [])
                reply[key] = [name] if name in legal and name in reply.get("speech", "") and rng.random() < 0.3 else []
            elif key == "ready_to_vote":
                match = _ROUND_RE.search(prompt)
                rnd = int(match.group(1)) if match else 1
                reply[key] = rng.random() < 0.15 + 0.25 * (rnd - 1)
            elif key == "reason":
                reply[key] = rng.choice(_REASONS)
            elif key == "thought":
                reply[key] = rng.choice(_THOUGHTS).format(name=name)
            elif key == "notes":
                reply[key] = rng.choice(_NOTES).format(name=name)
            else:
                reply[key] = ""

        if self.error_rate and rng.random() < self.error_rate:
            self.errors += 1
            if rng.random() < 0.5:
                raise BackendError("simulated backend failure (mock)")
            # An invalid reply: an illegal target, or a required field left out.
            if "target" in reply and rng.random() < 0.5:
                reply["target"] = "Nobody"
            else:
                required = [k for k in ("speech", "target", "reason", "urge") if k in reply]
                if required:
                    del reply[rng.choice(required)]
        return reply

    def _urge(self, system: str, prompt: str) -> int:
        """Random, but a Loudmouth leans high, a Quiet One low, and a direct question always gets 10."""
        if "asked you directly" in prompt:
            return 10
        match = _ARCHETYPE_RE.search(system)
        archetype = match.group(1) if match else ""
        if archetype == "Loudmouth":
            return self.rng.randint(5, 10)
        if archetype == "Quiet One":
            return self.rng.randint(0, 4)
        return self.rng.randint(0, 10)

    @staticmethod
    def _others(prompt: str) -> list[str]:
        """The living players other than the speaker, read from the turn prompt's "Alive" line."""
        match = _ALIVE_RE.search(prompt)
        if not match:
            return []
        names = []
        for part in match.group(1).split(","):
            part = part.strip()
            if part.endswith("(you)"):
                continue
            names.append(re.sub(r"\s*\(.*?\)\s*$", "", part))
        return [n for n in names if n]
