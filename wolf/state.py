"""Shared data types. See DESIGN.md for the event catalog and how the modules fit together."""

from dataclasses import asdict, dataclass, field

from .personalities import Persona
from .roles import Role

# Event.visible_to values: None = everyone; () = spectator only; ("Alice",) = only those players.
PUBLIC = None
SPECTATOR: tuple[str, ...] = ()


@dataclass
class Player:
    name: str
    role: Role
    persona: Persona
    color: str
    alive: bool = True
    notes: str = ""  # the agent's rolling private memory, rewritten every turn
    fate: str | None = None  # e.g. "killed by the werewolves on Night 1"
    # Doctor memory
    protected: list[tuple[int, str]] = field(default_factory=list)  # (night, name)
    self_protect_used: bool = False
    # Sheriff memory
    investigations: dict[str, bool] = field(default_factory=dict)  # name -> is_wolf

    @property
    def is_wolf(self) -> bool:
        return self.role is Role.WEREWOLF


@dataclass
class Event:
    kind: str
    day: int
    phase: str  # "setup" | "night" | "day" | "end"
    text: str = ""
    actor: str | None = None
    target: str | None = None
    visible_to: tuple[str, ...] | None = PUBLIC
    data: dict = field(default_factory=dict)

    @property
    def public(self) -> bool:
        return self.visible_to is None

    def visible(self, name: str) -> bool:
        return self.visible_to is None or name in self.visible_to

    def to_dict(self) -> dict:
        d = asdict(self)
        d["visible_to"] = None if self.visible_to is None else list(self.visible_to)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        vis = d.get("visible_to")
        return cls(
            kind=d["kind"],
            day=d["day"],
            phase=d["phase"],
            text=d.get("text", ""),
            actor=d.get("actor"),
            target=d.get("target"),
            visible_to=None if vis is None else tuple(vis),
            data=d.get("data") or {},
        )


@dataclass
class Task:
    """One decision the engine asks an agent to make."""

    kind: str  # discuss | speak | wolf_chat | wolf_pick | protect | investigate | vote | runoff_vote | defense | last_words
    header: str  # e.g. "Day 2, discussion round 1"
    instructions: str  # the "Your task" text shown to the agent
    targets: list[str] | None = None  # if set, the reply needs "target", one of these names
    speech_words: int | None = None  # if set, the reply needs "speech" of at most this many words
    reason_words: int | None = None  # if set, the reply needs a short public "reason"
    urge: bool = False  # discussion turn: the reply needs "urge" (0-10) and "ready_to_vote"
    asks: list[str] | None = None  # if set, the reply needs "asks", a list of these names
    private_fields: bool = True  # the reply has "thought" and "notes" (not when speaking after the dice)


@dataclass
class Request:
    system: str
    prompt: str
    schema: dict


@dataclass
class Decision:
    thought: str = ""
    notes: str = ""
    speech: str = ""
    target: str | None = None
    reason: str = ""
    urge: int | None = None  # discussion: 0-10, the chance of getting the floor, in tenths
    asks: list[str] = field(default_factory=list)  # speech after the dice: players expected to answer next
    ready_to_vote: bool | None = None  # discussion: None = no change (e.g. a fallback)
    auto: bool = False  # chosen without calling the model (only one legal option)
    fallback: bool = False  # every attempt failed; a random legal choice was made
    backend_error: bool = False  # the fallback was caused by backend failures, not bad replies
    error: str = ""


@dataclass
class Discussion:
    """Today's discussion so far. Everything here is public except `hesitated`, which each player
    only sees for themself."""

    round: int = 0
    spoken: dict[str, int] = field(default_factory=dict)  # name -> speeches heard today
    ready: dict[str, bool] = field(default_factory=dict)  # name -> latest ready_to_vote
    asked_by: dict[str, list[str]] = field(default_factory=dict)  # name -> who asked them, until their next turn
    hesitated: set[str] = field(default_factory=set)  # wanted to speak on their last turn, but didn't get the floor


# Per-player display colors (rich style names), assigned by seat. Red is reserved for deaths and wolves.
PLAYER_COLORS = [
    "cyan", "magenta", "yellow", "green", "bright_blue", "orange1", "orchid", "spring_green2",
    "gold1", "turquoise2", "hot_pink", "light_slate_blue", "chartreuse2", "salmon1",
    "deep_sky_blue1", "plum1", "khaki1", "aquamarine1", "light_coral", "medium_purple1",
]
