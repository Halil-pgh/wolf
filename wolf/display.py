"""Rich terminal output for the spectator.

`Display` is an engine observer (see DESIGN.md). It renders events from their structured fields
in one of two views:

* ``god``: every event, including thoughts and night secrets. Private events sit in a tagged,
  side-barred section ("🐺 wolf den", "🩺 private", "⭐ private") and use italic styles, so they
  never look like public talk.
* ``public``: only public events (``visible_to is None``), with no role emojis next to names,
  until ``game_over`` reveals everything.

A ``thought`` event is held back until the action it belongs to (the next event by the same
player) and printed right above that action, inside the same block. A discussion ``turn`` (the
thought and the dice) is printed as soon as it is rolled; when the player got the floor, their
``speech`` arrives after a second call and continues the same block. Silent turns and scores exist
only in god view: ``turn`` events are spectator-only.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass

from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.constrain import Constrain
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .roles import Role
from .state import Event

# After these kinds the display sleeps `delay` seconds (replays and mock games).
TALK_KINDS = frozenset(
    {"speech", "wolf_chat", "vote", "defense", "last_words", "announce", "death", "discussion_end"}
)
# Player actions that absorb the pending thought of the same player. ("pass" is from older logs.)
ACTION_KINDS = frozenset(
    {"turn", "speech", "pass", "wolf_chat", "wolf_pick", "protect", "investigate", "vote", "defense", "last_words"}
)
# Event blocks never get wider than this, so lines stay readable on very wide terminals.
MAX_BLOCK_WIDTH = 110
# Private sections: tag and color.
SECTIONS = {
    "wolf": ("🐺 wolf den", "indian_red"),
    "doctor": ("🩺 private", "green"),
    "sheriff": ("⭐ private", "yellow"),
}
SECTION_OF = {
    "wolf_chat": "wolf",
    "wolf_pick": "wolf",
    "wolf_decision": "wolf",
    "protect": "doctor",
    "investigate": "sheriff",
}
WINNER_STYLE = {
    "wolves": ("🐺  THE WEREWOLVES WIN  🐺", "red"),
    "village": ("🌾  THE VILLAGE WINS  🌾", "green"),
    None: ("🤝  DRAW  🤝", "yellow"),
}


@dataclass
class Seat:
    """What the display knows about one player (from the setup event)."""

    name: str
    color: str = "white"
    role: Role | None = None
    job: str = ""
    archetype: str = ""
    quirk: str = ""
    fate: str | None = None


def _s(value) -> str:
    return "" if value is None else str(value)


def _role(value) -> Role | None:
    if isinstance(value, Role):
        return value
    try:
        return Role(value)
    except ValueError:
        return None


def _color(value) -> str:
    """A rich color name for a player, or white if the value isn't one."""
    try:
        Style.parse(str(value))
        return str(value) if value else "white"
    except Exception:
        return "white"


def fmt_duration(seconds: float) -> str:
    """812.4 -> '13:32'; 3725 -> '1:02:05'."""
    total = int(round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


COST_LABEL = "API-equivalent cost (covered by your Claude plan)"
_STATS_KNOWN = {"calls", "errors", "seconds", "wall_seconds", "tokens", "input_tokens", "output_tokens", "cost_usd"}


def format_stats(stats: dict) -> tuple[list[tuple[str, str]], str | None]:
    """Backend stats as display pieces: ([(text, rich style), ...], cost line or None).

    Known keys get friendly wording (seconds as m:ss); any other scalar key is shown as "key: value".
    """

    def num(key):
        value = stats.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    parts: list[tuple[str, str]] = []
    if (calls := num("calls")) is not None:
        parts.append((f"{int(calls):,} call{'' if calls == 1 else 's'}", "bold"))
    if (errors := num("errors")) is not None:
        parts.append((f"{int(errors):,} error{'' if errors == 1 else 's'}", "bold red" if errors else "green"))
    if (secs := num("seconds")) is not None:
        parts.append((f"{fmt_duration(secs)} in model calls", ""))
    if (wall := num("wall_seconds")) is not None:
        parts.append((f"{fmt_duration(wall)} game time", ""))
    tin, tout, total = num("input_tokens"), num("output_tokens"), num("tokens")
    if total is None and (tin is not None or tout is not None):
        total = (tin or 0) + (tout or 0)
    if total is not None:
        tokens = f"{int(total):,} tokens"
        if tin is not None or tout is not None:
            tokens += f" ({int(tin or 0):,} in / {int(tout or 0):,} out)"
        parts.append((tokens, ""))
    for key, value in stats.items():
        if key in _STATS_KNOWN or value is None or isinstance(value, (dict, list, tuple)):
            continue
        if isinstance(value, float):
            value = f"{value:,.2f}"
        elif isinstance(value, int) and not isinstance(value, bool):
            value = f"{value:,}"
        parts.append((f"{str(key).replace('_', ' ')}: {value}", ""))
    cost = num("cost_usd")
    return parts, (None if cost is None else f"${cost:,.2f} {COST_LABEL}")


def role_summary(config: dict) -> str:
    """'2 Werewolves, 1 Doctor, ...' from a GameConfig.to_dict(), or '' if it can't be built."""
    try:
        from .config import GameConfig

        names = {f.name for f in dataclasses.fields(GameConfig)}
        return GameConfig(**{k: v for k, v in config.items() if k in names}).role_summary()
    except Exception:
        return ""


class _Gutter:
    """Renders `renderable` with `prefix` in front of every line: a continuous side bar."""

    def __init__(self, renderable: RenderableType, prefix: Text):
        self.renderable = renderable
        self.prefix = prefix

    def __rich_console__(self, console, options):
        width = max(1, options.max_width - self.prefix.cell_len)
        lines = console.render_lines(self.renderable, options.update(width=width), pad=False)
        prefix = list(self.prefix.render(console))
        for line in lines:
            yield from prefix
            yield from line
            yield Segment.line()


class _Busy:
    """Context manager behind Display.busy(): one spinner at a time; nested calls just relabel it."""

    def __init__(self, display: "Display", text: str):
        self.display = display
        self.text = text
        self.status = None
        self.previous = None

    def _label(self, text: str) -> Text:
        return Text(text, style="italic grey62")

    def __enter__(self):
        d = self.display
        d._flush()
        if d._status is not None:  # nested: relabel the running spinner
            self.previous = d._status_text
            d._status.update(self._label(self.text))
            d._status_text = self.text
            return self
        try:
            self.status = d.console.status(self._label(self.text), spinner="dots", spinner_style="cyan")
            self.status.start()
        except Exception:  # e.g. another live display is active: just skip the spinner
            self.status = None
            return self
        d._status, d._status_text = self.status, self.text
        return self

    def __exit__(self, *exc):
        d = self.display
        if self.status is not None:
            self.status.stop()
            d._status = d._status_text = None
        elif self.previous is not None and d._status is not None:
            d._status.update(self._label(self.previous))
            d._status_text = self.previous
        return False


class Display:
    """Engine observer that draws the game with rich. See the module docstring."""

    def __init__(self, console=None, view: str = "god", pause: bool = False, delay: float = 0.0,
                 show_notes: bool = False):
        if view not in ("god", "public"):
            raise ValueError(f"view must be 'god' or 'public', not {view!r}")
        self.console: Console = console or Console()
        self.view = view
        self.pause = pause
        self.delay = max(0.0, float(delay or 0.0))
        self.show_notes = show_notes
        self.roster: dict[str, Seat] = {}  # seat order
        self.config: dict = {}
        self.finished = False  # True once game_over or aborted was shown
        self._pending: Event | None = None  # thought waiting for its action
        self._talker: str | None = None  # whose discussion block was printed last
        self._floor = False  # that block was a turn that won the floor: its speech continues it
        self._section: str | None = None  # open private section
        self._subhead = None  # key of the last sub-header (discussion round, vote, ...)
        self._fresh = True  # nothing printed since the last header: no spacer needed
        self._phases = 0
        self._name_w = 10
        self._voter_w = 9
        self._status = None
        self._status_text = None
        self._handlers = {
            "setup": self._on_setup,
            "phase": self._on_phase,
            "wolf_chat": self._on_wolf_chat,
            "wolf_pick": self._on_wolf_pick,
            "wolf_decision": self._on_wolf_decision,
            "protect": self._on_protect,
            "investigate": self._on_investigate,
            "saved": self._on_saved,
            "announce": self._on_announce,
            "death": self._on_death,
            "turn": self._on_turn,
            "speech": self._on_speech,
            "ready": self._on_ready,
            "discussion_end": self._on_discussion_end,
            "pass": self._on_pass,
            "vote": self._on_vote,
            "vote_result": self._on_vote_result,
            "defense": self._on_defense,
            "last_words": self._on_last_words,
            "fallback": self._on_fallback,
            "game_over": self._on_game_over,
            "aborted": self._on_aborted,
        }

    # ------------------------------------------------------------------ observer API

    @property
    def god(self) -> bool:
        return self.view == "god"

    def visible(self, event: Event) -> bool:
        """God view sees everything; public view sees public events (and setup, for the roster)."""
        return self.god or event.kind == "setup" or event.visible_to is None

    def on_event(self, event: Event) -> None:
        if not self.visible(event):
            return
        try:
            self._dispatch(event)
        except Exception as exc:  # a rendering problem must never stop the game
            self._pending = None
            self._generic(event, error=exc)
        if self.delay > 0 and event.kind in TALK_KINDS:
            time.sleep(self.delay)

    def busy(self, text: str) -> _Busy:
        """`with display.busy("Alice is thinking…"):` shows a spinner while agents think."""
        return _Busy(self, text)

    def summary(self, stats: dict) -> None:
        """One compact block with whatever backend stats are present."""
        self._flush()
        parts, cost = format_stats(stats or {})
        if not parts and cost is None:
            return
        lines: list[RenderableType] = []
        if parts:
            lines.append(Text(" · ", style="dim").join(Text(text, style=style) for text, style in parts))
        if cost is not None:
            amount, label = cost.split(" ", 1)
            lines.append(Text.assemble((amount, "bold"), (" " + label, "dim")))
        self.console.print()
        self.console.rule(Text("📊 Stats", style="bold"), style="grey42")
        self.console.print(Padding(Group(*lines), (0, 0, 0, 2)))

    # ------------------------------------------------------------------ plumbing

    def _dispatch(self, e: Event) -> None:
        kind = e.kind
        if kind == "thought":
            self._flush()
            self._pending = e
            return
        if kind == "fallback":
            if self._pending is not None and self._pending.actor != e.actor:
                self._flush()
            self._on_fallback(e, None)
            return
        thought = None
        if kind in ACTION_KINDS and self._pending is not None and self._pending.actor == e.actor:
            thought, self._pending = self._pending, None
        else:
            self._flush()
        handler = self._handlers.get(kind)
        if handler is None:
            self._generic(e)
        else:
            handler(e, thought)
        if kind not in ("turn", "speech", "ready"):
            self._talker = None

    def _flush(self) -> None:
        """Print a thought whose action never came (it gets its own small block)."""
        thought, self._pending = self._pending, None
        if thought is None:
            return
        items = self._thought_items(thought)
        if items:
            label = self._name(thought.actor)
            label.stylize("dim")
            self._emit(self._block(label, items), section=self._section)

    def _emit(self, renderable: RenderableType, section: str | None = None, gap: bool = False) -> None:
        """Print one block: indented, or inside a private section's side bar."""
        c = self.console
        renderable = Constrain(renderable, MAX_BLOCK_WIDTH)
        if section != self._section:
            self._section = section
            if section:
                label, color = SECTIONS[section]
                if not self._fresh:
                    c.print()
                c.print(Text("  " + label, style=f"bold {color}"))
                self._fresh = True
        if section:
            _, color = SECTIONS[section]
            prefix = Text.assemble("  ", ("│", f"dim {color}"), " ")
            if gap and not self._fresh:
                c.print(_Gutter(Text(""), prefix))
            c.print(_Gutter(renderable, prefix))
        else:
            if gap and not self._fresh:
                c.print()
            c.print(Padding(renderable, (0, 0, 0, 2)))
        self._fresh = False

    def _subheader(self, key, label: str) -> None:
        if key == self._subhead:
            return
        self._subhead = key
        self._section = None
        self._talker, self._floor = None, False  # a new header ends the last block
        self.console.print()
        self.console.print(Text(f"  ─── {label} ───", style="bold grey58"))
        self._fresh = True

    def _generic(self, e: Event, error: Exception | None = None) -> None:
        """Unknown kinds (and events that failed to render): a plain line that can't fail."""
        try:
            line = Text("  · ", style="dim")
            line.append(f"[{e.kind}] ", style="dim")
            if e.actor:
                line.append_text(self._name(e.actor))
                line.append(" ")
            if e.target:
                line.append("→ ", style="dim")
                line.append_text(self._name(e.target))
                line.append(" ")
            line.append(_s(e.text))
            if error is not None:
                line.append(f"  (display error: {error})", style="dim red")
            self.console.print(line)
        except Exception:
            self.console.print(Text(f"  [{getattr(e, 'kind', '?')}] {getattr(e, 'text', '')}"))
        self._fresh = False

    # ------------------------------------------------------------------ building blocks

    def _seat(self, name: str | None) -> Seat | None:
        return self.roster.get(name) if name else None

    def _name(self, name: str | None, emoji: bool = True) -> Text:
        """Bold, in the player's color; in god view followed by the role emoji."""
        if not name:
            return Text("nobody", style="dim")
        seat = self._seat(name)
        text = Text(name, style=f"bold {seat.color}" if seat else "bold")
        if emoji and self.god and seat and seat.role:
            text.append(" " + seat.role.emoji)
        return text

    def _label(self, name: str | None, archetype: bool = True) -> Text:
        """Name column of a block: the name, with the archetype under it."""
        text = self._name(name)
        seat = self._seat(name)
        if archetype and self.god and seat and seat.archetype:
            text.append("\n")
            text.append(seat.archetype, style="dim")
        return text

    def _block(self, label: Text, items: list[RenderableType]) -> Table:
        """Name column on the left, the content wrapped (hanging) on the right."""
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(width=self._name_w, no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")  # "fold": rich wrongly ellipsizes wrapped lines holding wide emoji
        grid.add_row(label, Group(*items))
        return grid

    def _icon_line(self, icon: str, text: Text) -> Table:
        """`icon text…` with wrapped lines hanging under the text, not under the icon."""
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(width=cell_len(icon), no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")
        grid.add_row(icon, text)
        return grid

    def _thought_items(self, thought: Event | None) -> list[Text]:
        if thought is None:
            return []
        items = []
        if _s(thought.text).strip():
            items.append(Text("💭 " + _s(thought.text).strip(), style="dim italic"))
        notes = _s((thought.data or {}).get("notes")).strip()
        if self.show_notes and notes:
            items.append(Text("📝 " + notes, style="italic grey42"))
        return items

    def _role_text(self, role: Role | None, fallback: str = "") -> Text:
        if role is None:
            return Text(fallback or "?", style="bold")
        return Text(f"{role.emoji} {role.value}", style=f"bold {role.color}")

    # ------------------------------------------------------------------ setup & phases

    def _on_setup(self, e: Event, _thought) -> None:
        data = e.data or {}
        self.config = dict(data.get("config") or {})
        self.roster = {}
        for p in data.get("players") or []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            self.roster[p["name"]] = Seat(
                name=p["name"], color=_color(p.get("color")), role=_role(p.get("role")),
                job=_s(p.get("job")), archetype=_s(p.get("archetype")), quirk=_s(p.get("quirk")),
            )
        names = [cell_len(self._name(n).plain) for n in self.roster]
        archetypes = [cell_len(s.archetype) for s in self.roster.values()] if self.god else []
        self._voter_w = max(4, min(16, max(names, default=8)))
        self._name_w = max(8, min(16, max(names + archetypes, default=10)))
        self._banner()
        self._roster_table()
        self._fresh = True

    def _banner(self) -> None:
        cfg = self.config
        title = Text("🐺  W  O  L  F  🌕", style="bold", justify="center")
        info = []
        if self.roster:
            info.append(f"{len(self.roster)} players")
        if summary := role_summary(cfg):
            info.append(summary)
        if cfg.get("seed") is not None:
            info.append(f"seed {cfg['seed']}")
        lines = [title]
        if info:
            lines.append(Text(" · ".join(info), style="grey70", justify="center"))
        if self.god:
            view = "God view: you see every role, thought and secret."
        else:
            view = "Public view: you see only what the village sees, until the end."
        lines.append(Text(view, style="italic grey58", justify="center"))
        self.console.print(Panel(Group(*lines), box=box.DOUBLE, border_style="red", padding=(1, 2)))

    def _roster_table(self) -> None:
        if not self.roster:
            return
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", show_edge=False, pad_edge=False,
                      title="The villagers", title_style="bold", expand=False)
        table.add_column("Name", no_wrap=True)
        table.add_column("Job", no_wrap=True)
        table.add_column("Personality", no_wrap=True)
        table.add_column("Quirk", ratio=1, overflow="fold")
        if self.god:
            table.add_column("Role", no_wrap=True)
        for seat in self.roster.values():
            row = [self._name(seat.name, emoji=False), seat.job.capitalize(), seat.archetype,
                   Text(seat.quirk, style="grey70")]
            if self.god:
                row.append(self._role_text(seat.role))
            table.add_row(*row)
        self.console.print(Padding(table, (0, 0, 0, 2)))
        if self.god:
            groups: dict[Role, list[str]] = {}
            for seat in self.roster.values():
                if seat.role and seat.role is not Role.VILLAGER:
                    groups.setdefault(seat.role, []).append(seat.name)
            order = [Role.WEREWOLF, Role.DOCTOR, Role.SHERIFF]
            groups = {r: groups[r] for r in order if r in groups}
            if groups:
                line = Text("  ")
                for i, (role, names) in enumerate(groups.items()):
                    if i:
                        line.append("   ")
                    label = "Pack" if role is Role.WEREWOLF else role.value
                    line.append(f"{role.emoji} {label}: ", style=f"bold {role.color}")
                    line.append_text(Text(", ").join(self._name(n, emoji=False) for n in names))
                self.console.print(line)

    def _on_phase(self, e: Event, _thought) -> None:
        if self.pause:
            prompt = "Press Enter to start the game…" if self._phases == 0 else "Press Enter to continue…"
            self._wait(prompt)
        self._phases += 1
        night = e.phase == "night"
        icon, color = ("🌙", "bright_blue") if night else ("☀️", "gold1")
        text = _s(e.text).strip() or f"{'Night' if night else 'Day'} {e.day}"
        self.console.print()
        self.console.rule(Text(f"{icon}  {text}", style=f"bold {color}"), style=color, characters="━")
        if night and not self.god:
            self.console.print(Text("  The village sleeps. Something moves in the dark…", style="dim italic"))
        self._section = None
        self._subhead = None
        self._fresh = True

    def _wait(self, prompt: str) -> None:
        try:
            self.console.input(Text(prompt, style="dim italic"))
        except EOFError:  # no stdin (piped): stop pausing
            self.pause = False
            self.console.print()
            return
        if self.console.is_terminal:  # erase the prompt line
            self.console.file.write("\x1b[1A\x1b[2K")
            self.console.file.flush()

    # ------------------------------------------------------------------ night

    def _on_wolf_chat(self, e: Event, thought) -> None:
        said = Text()
        if e.target:
            said.append("→ ", style="bold")
            said.append_text(self._name(e.target))
            said.append("  ")
        said.append(f"“{_s(e.text).strip()}”", style="italic")
        items = self._thought_items(thought) + [said]
        self._emit(self._block(self._label(e.actor), items), section="wolf", gap=True)

    def _on_wolf_pick(self, e: Event, thought) -> None:
        pick = Text.assemble(("picks ", "italic"), self._name(e.target))
        items = self._thought_items(thought) + [pick]
        self._emit(self._block(self._label(e.actor, archetype=False), items), section="wolf",
                   gap=bool(thought))

    def _on_wolf_decision(self, e: Event, _thought) -> None:
        line = Text.assemble(("The pack will attack ", "bold indian_red"), self._name(e.target),
                             (".", "bold indian_red"))
        if (e.data or {}).get("random"):
            line.append("  (the picks were split; chosen at random)", style="dim italic")
        self._emit(self._icon_line("🐺", line), section="wolf", gap=True)

    def _on_protect(self, e: Event, thought) -> None:
        act = Text.assemble(("protects ", "italic"), self._name(e.target))
        items = self._thought_items(thought) + [act]
        self._emit(self._block(self._label(e.actor, archetype=False), items), section="doctor",
                   gap=bool(thought))

    def _on_investigate(self, e: Event, thought) -> None:
        act = Text.assemble(("investigates ", "italic"), self._name(e.target), ("  →  ", "dim"))
        wolf = (e.data or {}).get("wolf")
        if wolf is None:
            act.append("?", style="bold")
        elif wolf:
            act.append("🐺 WOLF", style="bold red")
        else:
            act.append("not a wolf", style="bold green")
        items = self._thought_items(thought) + [act]
        self._emit(self._block(self._label(e.actor, archetype=False), items), section="sheriff",
                   gap=bool(thought))

    def _on_saved(self, e: Event, _thought) -> None:
        line = Text.assemble(("The wolves attacked ", "italic green"), self._name(e.target),
                             (", but the Doctor's protection saved them.", "italic green"))
        self._emit(self._icon_line("✨", line))

    # ------------------------------------------------------------------ day

    def _on_announce(self, e: Event, _thought) -> None:
        self._emit(self._icon_line("📢", Text(_s(e.text).strip(), style="bold")), gap=True)

    def _on_death(self, e: Event, _thought) -> None:
        data = e.data or {}
        seat = self._seat(e.target)
        role = _role(data.get("role")) or (seat.role if seat else None)
        fate = _s(data.get("fate")).strip()
        if seat:
            seat.fate = fate or seat.fate
        line = Text.assemble(self._name(e.target, emoji=False), (" was the ", "bold red"),
                             self._role_text(role, _s(data.get("role"))))
        if fate:
            line.append(f"  ({fate})", style="red dim")
        self._emit(self._icon_line("💀", line))

    def _round_label(self, e: Event) -> tuple:
        rnd = (e.data or {}).get("round")
        if rnd is None:
            return ("talk", e.day, None), "Discussion"
        return ("talk", e.day, rnd), f"Discussion · round {rnd}"

    def _turn_line(self, turn: Event) -> Text:
        """God view: '🙋 7/10 · rolled 3' or '🤐 2/10 · rolled 7 · stays quiet'."""
        data = turn.data or {}
        urge, roll, spoke = data.get("urge"), data.get("roll"), bool(data.get("spoke"))
        line = Text("🙋 " if spoke else "🤐 ", style="dim")
        line.append(f"{_s(urge) or '?'}/10", style="bold" if spoke else "dim")
        if isinstance(urge, int) and 0 < urge < 10 and roll is not None:
            line.append(f" · rolled {roll}", style="dim")
        if spoke and data.get("reply_to"):
            line.append(" · ", style="dim")
            line.append_text(self._reply_mark(data["reply_to"]))
        if not spoke:
            line.append(" · stays quiet", style="dim italic")
            if data.get("reply_to"):
                line.append(" · doesn't answer ", style="dim italic")
                line.append_text(self._name(data["reply_to"], emoji=False))
        return line

    def _reply_mark(self, asker: str) -> Text:
        return Text.assemble(("↩ answers ", "dim italic"), self._name(asker, emoji=False))

    def _on_turn(self, e: Event, thought) -> None:
        """The thought and the dice, printed at once; a speech that wins the floor continues this block."""
        spoke = bool((e.data or {}).get("spoke"))
        self._subheader(*self._round_label(e))
        label = self._label(e.actor, archetype=spoke)
        if not spoke:
            label.stylize("dim")
        self._emit(self._block(label, self._thought_items(thought) + [self._turn_line(e)]), gap=True)
        self._talker, self._floor = e.actor, spoke

    def _on_speech(self, e: Event, thought) -> None:
        said = Text(_s(e.text).strip() or "…")
        if self._floor and self._talker == e.actor:  # god view: under its turn
            self._emit(self._block(Text(""), self._thought_items(thought) + [said]))
        else:  # public view (or an old log): a block of its own
            self._subheader(*self._round_label(e))
            items = self._thought_items(thought)
            if reply_to := (e.data or {}).get("reply_to"):
                items.append(self._reply_mark(reply_to))
            self._emit(self._block(self._label(e.actor), items + [said]), gap=True)
        self._talker, self._floor = e.actor, False

    def _on_ready(self, e: Event, _thought) -> None:
        """Under the player's own block when it was just printed; otherwise a line of its own
        (in public view, a silent player's change of mind)."""
        data = e.data or {}
        self._subheader(*self._round_label(e))
        line = Text.assemble(self._name(e.actor, emoji=False),
                             (" is ready to vote" if data.get("ready") else " is no longer ready to vote", "italic"))
        if isinstance(data.get("count"), int) and isinstance(data.get("living"), int):
            line.append(f"  ({data['count']} of {data['living']} ready)", style="dim")
        if e.actor is not None and e.actor == self._talker:
            self._emit(self._block(Text(""), [self._icon_line("✋", line)]))
        else:
            self._emit(self._icon_line("✋", line), gap=self._talker is not None)
            self._talker = None

    def _on_discussion_end(self, e: Event, _thought) -> None:
        self._emit(self._icon_line("🔔", Text(_s(e.text).strip() or "Time to vote.", style="bold")), gap=True)

    def _on_pass(self, e: Event, thought) -> None:
        self._subheader(*self._round_label(e))
        label = self._label(e.actor, archetype=False)
        label.stylize("dim")
        items = self._thought_items(thought) + [Text("stays silent.", style="dim italic")]
        self._emit(self._block(label, items), gap=True)

    def _on_vote(self, e: Event, thought) -> None:
        data = e.data or {}
        runoff = bool(data.get("runoff"))
        self._subheader(("vote", e.day, runoff), "Runoff vote" if runoff else "Vote")
        parts: list[RenderableType] = []
        if items := self._thought_items(thought):
            parts.append(Padding(Group(*items), (0, 0, 0, 3)))
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(width=self._voter_w, no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")
        line = Text.assemble(("→  ", "bold"), self._name(e.target))
        reason = _s(data.get("reason")).strip()
        if data.get("auto"):
            reason = reason or "(only choice)"
        if reason:
            line.append(" · ", style="dim")
            line.append(reason, style="dim italic" if data.get("auto") else "italic")
        if runoff:
            line.append("  ⟲ runoff", style="magenta dim")
        grid.add_row("🗳️", self._name(e.actor), line)
        parts.append(grid)
        self._emit(Group(*parts), gap=bool(thought))

    def _on_vote_result(self, e: Event, _thought) -> None:
        data = e.data or {}
        tally = {k: v for k, v in (data.get("tally") or {}).items() if isinstance(v, (int, float))}
        text = _s(e.text).strip()
        if not text:
            text = ", ".join(f"{n} {c}" for n, c in sorted(tally.items(), key=lambda kv: -kv[1]))
        head = Text(text, style="bold")
        if data.get("runoff"):
            head.append("  (runoff)", style="magenta dim")
        parts: list[RenderableType] = [self._icon_line("⚖️", head)]
        if tally:
            tied = set(data.get("tied") or [])
            grid = Table.grid(padding=(0, 1))
            grid.add_column(no_wrap=True)
            grid.add_column(no_wrap=True)
            grid.add_column(no_wrap=True, justify="right")
            grid.add_column(no_wrap=True)
            for name, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
                seat = self._seat(name)
                color = seat.color if seat else "white"
                bar = Text("█" * min(int(count), 30) or "·", style=color)
                mark = Text("tied", style="yellow dim") if name in tied else Text("")
                grid.add_row(self._name(name), bar, Text(str(count), style="bold"), mark)
            parts.append(Padding(grid, (0, 0, 0, 3)))
        self._emit(Group(*parts), gap=True)

    def _on_defense(self, e: Event, thought) -> None:
        self._subheader(("defense", e.day), "Tie · defenses")
        said = Text.assemble(("Defense: ", "bold"), _s(e.text).strip() or "…")
        items = self._thought_items(thought) + [said]
        self._emit(self._block(self._label(e.actor), items), gap=True)

    def _on_last_words(self, e: Event, thought) -> None:
        said = Text.assemble(("Last words: ", "bold"), (_s(e.text).strip() or "…", "italic"))
        items = self._thought_items(thought) + [said]
        self._section = None
        self._emit(self._block(self._label(e.actor), items), gap=True)

    def _on_fallback(self, e: Event, _thought) -> None:
        line = Text.assemble(self._name(e.actor), (": " + _s(e.text).strip(), "yellow"))
        self._emit(self._icon_line("⚠", line), section=self._section)

    # ------------------------------------------------------------------ the end

    def _on_game_over(self, e: Event, _thought) -> None:
        data = e.data or {}
        winner = data.get("winner")
        headline, color = WINNER_STYLE.get(winner, WINNER_STYLE[None])
        body = [Text(headline, style=f"bold {color}", justify="center")]
        if text := _s(e.text).strip():
            body.append(Text(text, justify="center"))
        self.console.print()
        self.console.print(Panel(Group(*body), title="🏁 Game over", border_style=color, box=box.DOUBLE,
                                 padding=(1, 2)))
        self._final_table(data)
        self._section = None
        self.finished = True

    def _final_table(self, data: dict) -> None:
        roles = dict(data.get("roles") or {})
        fates = dict(data.get("fates") or {})
        names = list(self.roster) + [n for n in list(roles) + list(fates) if n not in self.roster]
        if not names:
            return
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", show_edge=False, pad_edge=False)
        table.add_column("Player", no_wrap=True)
        table.add_column("Role", no_wrap=True)
        table.add_column("Fate", ratio=1, overflow="fold")
        table.add_column("Survived", justify="center", no_wrap=True)
        for name in dict.fromkeys(names):
            seat = self._seat(name)
            role = _role(roles.get(name)) or (seat.role if seat else None)
            fate = fates[name] if name in fates else (seat.fate if seat else None)
            alive = fate is None
            table.add_row(
                self._name(name, emoji=False),
                self._role_text(role, _s(roles.get(name))),
                Text(_s(fate) or "survived", style="grey62" if fate else "green"),
                Text("✔", style="bold green") if alive else Text("✘", style="bold red"),
            )
        self.console.print(Padding(table, (0, 0, 0, 2)))

    def _on_aborted(self, e: Event, _thought) -> None:
        self.console.print()
        self.console.print(Panel(Text(_s(e.text).strip() or "The game was aborted."), title="⛔ Game aborted",
                                 border_style="red", box=box.HEAVY, padding=(1, 2)))
        self._section = None
        self.finished = True
