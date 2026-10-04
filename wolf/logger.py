"""Game logs: one JSONL file per game, plus a readable god-view Markdown transcript.

File format (one JSON object per line):

    {"type": "meta", "config": {...}, "backend": "claude", "model": "sonnet", "seed": 42, "started": "..."}
    {"type": "event", "kind": "setup", "day": 0, ...}     # Event.to_dict(), one line per event
    ...
    {"type": "stats", "calls": 97, "seconds": 812.4, ...}  # optional, written by close(stats)

`load_game(path)` returns `(meta, events)`. If the file has a stats line, the stats are returned
inside the meta dict as `meta["stats"]`. Lines that don't parse (for example a last line cut off by
a crash) are skipped, so a partial log still replays.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from .roles import Role
from .state import Event


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class GameLogger:
    """Engine observer that writes `<directory>/<YYYYmmdd-HHMMSS>.jsonl` (and the `.md` next to it)."""

    def __init__(self, directory: str | Path, meta: dict):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.meta = {k: v for k, v in dict(meta).items() if k != "type"}
        stamp = _timestamp()
        n = 1
        while True:
            path = self.directory / (f"{stamp}.jsonl" if n == 1 else f"{stamp}-{n}.jsonl")
            if not path.with_suffix(".md").exists():
                try:
                    self._file = open(path, "x", encoding="utf-8")
                    break
                except FileExistsError:
                    pass
            n += 1
        self.path: Path = path
        self.md_path: Path = path.with_suffix(".md")
        self.events: list[Event] = []
        self.closed = False
        self.transcript_written = False
        self._write({"type": "meta", **self.meta})

    def _write(self, record: dict) -> None:
        self._file.write(_dumps(record) + "\n")
        self._file.flush()

    def on_event(self, event: Event) -> None:
        if self.closed:
            return
        self.events.append(event)
        self._write({"type": "event", **event.to_dict()})
        if event.kind in ("game_over", "aborted"):
            self.write_transcript()

    def write_transcript(self) -> Path | None:
        """(Re)write the Markdown transcript. Never raises: a failed transcript must not stop the game."""
        try:
            write_markdown(self.meta, self.events, self.md_path)
        except Exception as exc:
            print(f"wolf: could not write the transcript {self.md_path}: {exc}", file=sys.stderr)
            return None
        self.transcript_written = True
        return self.md_path

    def close(self, stats: dict | None = None) -> None:
        """Append the stats line (if given), refresh the transcript, and close the file. Idempotent."""
        if self.closed:
            return
        try:
            if stats is not None:
                self.meta["stats"] = dict(stats)
                self._write({"type": "stats", **{k: v for k, v in stats.items() if k != "type"}})
            if self.events:  # includes stats, and covers games that stopped without game_over
                self.write_transcript()
        finally:
            self._file.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def load_game(path) -> tuple[dict, list[Event]]:
    """Read a JSONL game log. Returns (meta, events); stats, if logged, are in meta["stats"]."""
    meta: dict = {}
    events: list[Event] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                kind = record.pop("type", None)
                if kind == "meta":
                    meta.update(record)
                elif kind == "event":
                    events.append(Event.from_dict(record))
                elif kind == "stats":
                    meta["stats"] = record
            except (ValueError, KeyError, TypeError, AttributeError):
                continue  # a truncated or foreign line: skip it
    return meta, events


# ---------------------------------------------------------------------- Markdown transcript

_PRIVATE_SECTIONS = {
    "wolf_chat": ("wolf", "🐺 Wolf den (private)"),
    "wolf_pick": ("wolf", "🐺 Wolf den (private)"),
    "wolf_decision": ("wolf", "🐺 Wolf den (private)"),
    "protect": ("doctor", "🩺 Doctor (private)"),
    "investigate": ("sheriff", "⭐ Sheriff (private)"),
}
_ACTIONS = {"turn", "speech", "pass", "wolf_chat", "wolf_pick", "protect", "investigate", "vote", "defense",
            "last_words"}
_WINNER = {"wolves": "🐺 The werewolves win", "village": "🌾 The village wins", None: "🤝 Draw"}


def _inline(text) -> str:
    return " ".join(str(text or "").split())


def _cell(text) -> str:
    return _inline(text).replace("|", "\\|") or " "


def _role(value) -> Role | None:
    try:
        return Role(value)
    except ValueError:
        return None


def _role_label(value) -> str:
    role = _role(value)
    return f"{role.emoji} {role.value}" if role else _inline(value) or "?"


class _Markdown:
    """Builds the transcript. Blocks are separated by blank lines; consecutive list items stay tight."""

    def __init__(self, meta: dict, events: list[Event]):
        self.meta = meta
        self.events = events
        self.lines: list[str] = []
        self.last = None  # "item" | "block"
        self.section = None
        self.pending: Event | None = None
        self.turn: tuple[Event, Event | None] | None = None  # a speaking turn waiting for its speech
        self.roles: dict[str, str] = {}
        self.archetypes: dict[str, str] = {}

    # low-level writers
    def block(self, text: str) -> None:
        if self.lines:
            self.lines.append("")
        self.lines.append(text)
        self.last = "block"

    def item(self, text: str, thought: Event | None = None) -> None:
        if self.lines and self.last != "item":
            self.lines.append("")
        self.lines.append(f"- {text}")
        if thought is not None and _inline(thought.text):
            self.lines.append(f"  - 💭 *{_inline(thought.text)}*")
        self.last = "item"

    def heading(self, text: str, level: int = 2) -> None:
        self.block("#" * level + " " + text)
        self.section = None

    def subheading(self, key, text: str) -> None:
        if key != self.section:
            self.section = key
            self.block(f"**{text}**")

    def who(self, name: str | None) -> str:
        if not name:
            return "nobody"
        role = _role(self.roles.get(name))
        return f"**{name}**" + (f" {role.emoji}" if role else "")

    def quote_thought(self, thought: Event | None) -> None:
        if thought is not None and _inline(thought.text):
            self.block(f"> 💭 *{thought.actor} thinks:* {_inline(thought.text)}")

    def flush(self) -> None:
        if self.turn is not None:  # its speech never came
            (turn, thought), self.turn = self.turn, None
            self.quote_thought(thought)
            self.block(f"{self.who(turn.actor)} · {self.urge(turn)}")
        thought, self.pending = self.pending, None
        self.quote_thought(thought)

    @staticmethod
    def urge(turn: Event) -> str:
        """'🙋 7/10 (rolled 3)' or '🤐 2/10 (rolled 7)'."""
        data = turn.data or {}
        urge, roll = data.get("urge"), data.get("roll")
        text = f"{'🙋' if data.get('spoke') else '🤐'} {urge if urge is not None else '?'}/10"
        if isinstance(urge, int) and 0 < urge < 10 and roll is not None:
            text += f" (rolled {roll})"
        return text

    # document
    def render(self) -> str:
        setup = next((e for e in self.events if e.kind == "setup"), None)
        end = next((e for e in reversed(self.events) if e.kind in ("game_over", "aborted")), None)
        players = list((setup.data or {}).get("players") or []) if setup else []
        config = dict((setup.data or {}).get("config") or {}) if setup else dict(self.meta.get("config") or {})
        for p in players:
            self.roles[p.get("name")] = p.get("role")
            self.archetypes[p.get("name")] = p.get("archetype") or ""
        fates: dict[str, str | None] = {}
        for e in self.events:
            if e.kind == "death" and e.target:
                fates[e.target] = (e.data or {}).get("fate") or "dead"
        if end is not None and end.kind == "game_over":
            fates.update((end.data or {}).get("fates") or {})

        self.lines.append("# 🐺 Wolf · game transcript")
        facts = []
        if started := self.meta.get("started"):
            facts.append(f"**Started:** {started}")
        if backend := self.meta.get("backend"):
            extra = ", ".join(str(x) for x in (self.meta.get("model"), self.meta.get("effort")) if x)
            facts.append(f"**Backend:** {backend}" + (f" ({extra})" if extra else ""))
        seed = self.meta.get("seed", config.get("seed"))
        if seed is not None:
            facts.append(f"**Seed:** {seed}")
        if facts:
            self.block(" · ".join(facts))
        from .display import role_summary

        setup_line = []
        if players:
            setup_line.append(f"{len(players)} players")
        if summary := role_summary(config):
            setup_line.append(summary)
        if config.get("max_rounds") and config.get("max_speeches"):
            setup_line.append(f"discussion of up to {config['max_rounds']} rounds a day, "
                              f"{config['max_speeches']} speeches per player")
        if setup_line:
            self.block("**Setup:** " + " · ".join(setup_line))
        if end is not None:
            self.block("**Result:** " + self.result_line(end))

        if players:
            self.heading("Players")
            rows = ["| Player | Role | Job | Personality | Quirk | Fate |", "|---|---|---|---|---|---|"]
            for p in players:
                name = p.get("name")
                fate = fates.get(name) or "survived"
                rows.append(
                    f"| **{_cell(name)}** | {_cell(_role_label(p.get('role')))} | {_cell(str(p.get('job') or '').capitalize())}"
                    f" | {_cell(p.get('archetype'))} | {_cell(p.get('quirk'))} | {_cell(fate)} |"
                )
            self.block("\n".join(rows))

        for e in self.events:
            if e.kind == "setup":
                continue
            if e.kind == "thought":
                self.flush()
                self.pending = e
                continue
            if e.kind == "fallback" and self.turn is not None and self.turn[0].actor == e.actor:
                self.flush()  # their speech call failed: write the turn before the fallback
            thought = None
            if e.kind in _ACTIONS and self.pending is not None and self.pending.actor == e.actor:
                thought, self.pending = self.pending, None
            elif e.kind != "fallback" and not (e.kind == "speech" and self.turn and self.turn[0].actor == e.actor):
                self.flush()
            self.event(e, thought)
        self.flush()

        if end is None:
            self.heading("Result")
            self.block("*The game ended early; no result was recorded.*")
        if stats := self.meta.get("stats"):
            from .display import format_stats

            parts, cost = format_stats(stats)
            self.heading("📊 Stats")
            if parts:
                self.block(" · ".join(text for text, _ in parts))
            if cost:
                self.block(cost)
        return "\n".join(self.lines) + "\n"

    def result_line(self, end: Event) -> str:
        if end.kind == "aborted":
            return f"⛔ Aborted: {_inline(end.text)}"
        headline = _WINNER.get((end.data or {}).get("winner"), _WINNER[None])
        text = _inline(end.text)
        return f"{headline.split(' ', 1)[0]} {text}" if text else headline

    def event(self, e: Event, thought: Event | None) -> None:
        data = e.data or {}
        kind = e.kind
        if kind in _PRIVATE_SECTIONS:
            key, label = _PRIVATE_SECTIONS[kind]
            self.subheading((key, e.day), label)
        if kind == "phase":
            night = e.phase == "night"
            self.heading(f"{'🌙 Night' if night else '☀️ Day'} {e.day}")
            if _inline(e.text):
                self.block(f"*{_inline(e.text)}*")
        elif kind == "wolf_chat":
            target = f" → {self.who(e.target)}" if e.target else ""
            self.item(f"{self.who(e.actor)}{target}: “{_inline(e.text)}”", thought)
        elif kind == "wolf_pick":
            self.item(f"{self.who(e.actor)} picks {self.who(e.target)}", thought)
        elif kind == "wolf_decision":
            split = " *(the picks were split; chosen at random)*" if data.get("random") else ""
            self.item(f"**The pack will attack {e.target or 'nobody'}.**{split}")
        elif kind == "protect":
            self.item(f"{self.who(e.actor)} protects {self.who(e.target)}", thought)
        elif kind == "investigate":
            wolf = data.get("wolf")
            result = "?" if wolf is None else ("**🐺 Wolf**" if wolf else "**not a wolf**")
            self.item(f"{self.who(e.actor)} investigates {self.who(e.target)}: {result}", thought)
        elif kind == "saved":
            self.block(f"✨ *(private)* The wolves attacked **{e.target}**, but the Doctor's protection saved them.")
        elif kind == "announce":
            self.block(f"**📢 {_inline(e.text)}**")
        elif kind == "death":
            fate = f" ({_inline(data.get('fate'))})" if data.get("fate") else ""
            self.block(f"**💀 {e.target} was the {_role_label(data.get('role'))}**{fate}")
        elif kind in ("speech", "pass", "turn", "ready", "lean"):
            rnd = data.get("round")
            self.subheading(("talk", e.day, rnd), f"🗣️ Discussion · round {rnd}" if rnd else "🗣️ Discussion")
            if kind == "turn" and data.get("spoke"):
                self.turn = (e, thought)  # written with the speech that follows
            elif kind == "speech":
                turn = None
                if self.turn is not None and self.turn[0].actor == e.actor:
                    (turn, turn_thought), self.turn = self.turn, None
                    thought = thought or turn_thought
                self.quote_thought(thought)
                arch = self.archetypes.get(e.actor)
                notes = [self.urge(turn)] if turn is not None else []
                if data.get("reply_to"):
                    notes.append(f"↩ *answers {data['reply_to']}*")
                self.block(f"{self.who(e.actor)}" + (f" *({arch})*" if arch else "")
                           + "".join(f" · {n}" for n in notes) + f": {_inline(e.text)}")
            elif kind == "turn":
                self.quote_thought(thought)
                answer = f", not answering {data['reply_to']}" if data.get("reply_to") else ""
                self.block(f"*{self.urge(e)} · {e.actor} stays quiet{answer}.*")
            elif kind == "ready":
                state = "is ready to vote" if data.get("ready") else "is no longer ready to vote"
                count = f" ({data['count']} of {data['living']} ready)" if "count" in data and "living" in data else ""
                self.block(f"*✋ {e.actor} {state}{count}.*")
            elif kind == "lean":
                tally = data.get("tally") or {}
                counts = f" ({', '.join(f'{n} {c}' for n, c in tally.items())})" if tally else ""
                self.block(f"*👉 {e.actor} leans toward {e.target or 'nobody'}{counts}.*")
            else:
                self.quote_thought(thought)
                self.block(f"*{e.actor} stays silent.*")
        elif kind == "discussion_end":
            self.block(f"**🔔 {_inline(e.text)}**")
        elif kind == "trial":
            self.subheading(("defense", e.day, "trial"), "🎯 Defense before the vote")
            self.block(f"**{_inline(e.text)}**")
        elif kind == "vote":
            runoff = bool(data.get("runoff"))
            self.subheading(("vote", e.day, runoff), "🔁 Runoff vote" if runoff else "🗳️ Vote")
            reason = f" · {_inline(data.get('reason'))}" if _inline(data.get("reason")) else ""
            tags = (" *(runoff)*" if runoff else "") + (" *(only choice)*" if data.get("auto") else "")
            self.item(f"{self.who(e.actor)} → {self.who(e.target)}{reason}{tags}", thought)
        elif kind == "vote_result":
            self.block(f"**⚖️ {_inline(e.text)}**" + (" *(runoff)*" if data.get("runoff") else ""))
        elif kind == "defense":
            if data.get("trial"):
                self.subheading(("defense", e.day, "trial"), "🎯 Defense before the vote")
            else:
                self.subheading(("defense", e.day, "tie"), "🛡️ Tie · defenses")
            self.quote_thought(thought)
            self.block(f"{self.who(e.actor)} *(defense)*: {_inline(e.text)}")
        elif kind == "last_words":
            self.quote_thought(thought)
            self.block(f"{self.who(e.actor)} *(last words)*: {_inline(e.text)}")
        elif kind == "fallback":
            self.item(f"⚠ *fallback* · {self.who(e.actor)}: {_inline(e.text)}")
        elif kind == "game_over":
            self.heading("🏁 Result")
            self.block(f"**{self.result_line(e)}**")
            roles = data.get("roles") or self.roles
            fates = data.get("fates") or {}
            rows = ["| Player | Role | Fate | Survived |", "|---|---|---|---|"]
            for name in dict.fromkeys(list(self.roles) + list(roles)):
                fate = fates.get(name)
                rows.append(f"| **{_cell(name)}** | {_cell(_role_label(roles.get(name, self.roles.get(name))))}"
                            f" | {_cell(fate or 'survived')} | {'✘' if fate else '✔'} |")
            self.block("\n".join(rows))
        elif kind == "aborted":
            self.heading("⛔ Aborted")
            self.block(_inline(e.text) or "The game was aborted.")
        else:
            who = f"{self.who(e.actor)} " if e.actor else ""
            self.item(f"*[{kind}]* {who}{_inline(e.text)}")


def render_markdown(meta: dict, events: list[Event]) -> str:
    return _Markdown(meta or {}, list(events)).render()


def write_markdown(meta: dict, events: list[Event], path) -> None:
    """Write the god-view Markdown transcript of a game to `path`."""
    Path(path).write_text(render_markdown(meta, events), encoding="utf-8")
