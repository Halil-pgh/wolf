"""Engine, agent and hidden-information tests, all on the mock backend."""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter

import pytest

from wolf import prompts
from wolf.agent import Agent, parse_reply
from wolf.backends.base import BackendError
from wolf.backends.mock import MockBackend
from wolf.config import GameConfig
from wolf.engine import Game, GameAborted
from wolf.roles import Role, Team
from wolf.state import Decision, Event, Task

ACTING_KINDS = {"turn", "speech", "ready", "vote", "thought", "wolf_chat", "wolf_pick", "protect", "investigate",
                "defense", "last_words", "fallback"}


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------

class TaggedMock(MockBackend):
    """Tags every free-text field with a unique token, so leak checks can't collide on canned lines."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tags = itertools.count(1)

    def complete(self, system, prompt, schema):
        reply = super().complete(system, prompt, schema)
        n = next(self._tags)
        for key in ("thought", "speech", "notes"):
            if isinstance(reply.get(key), str) and reply[key]:
                reply[key] = f"{reply[key]} <{key}-{n}>"
        return reply


class Scripted(MockBackend):
    """Discussion turns come from `script(turn, turns) -> dict` (fields to override); every other task is
    played by the mock. By default a turn has urge 10, a speech, no asks, and isn't ready to vote.
    The first call of a turn gets thought/urge/ready_to_vote/notes; the speech call after the dice
    gets that turn's speech/asks."""

    def __init__(self, script=None, seed: int = 0):
        super().__init__(seed=seed)
        self.script = script or (lambda turn, turns: {})
        self.turns: list[dict] = []
        self.speak_prompts: list[str] = []
        self._planned: dict = {}

    def complete(self, system, prompt, schema):
        props = schema["properties"]
        if "asks" in props:  # the speech, after the dice
            self.calls += 1
            self.speak_prompts.append(prompt)
            return {"speech": self._planned["speech"], "asks": self._planned["asks"]}
        if "urge" not in props:
            return super().complete(system, prompt, schema)
        turn = {
            "name": re.match(r"You are (\w+),", system).group(1),
            "day": int(re.search(r"^# Day (\d+), discussion round (\d+)", prompt, re.M).group(1)),
            "round": int(re.search(r"^# Day (\d+), discussion round (\d+)", prompt, re.M).group(2)),
            "alive": int(re.search(r"^Alive \((\d+)\)", prompt, re.M).group(1)),
            "legal": self._others(prompt),  # the living players other than this one
            "prompt": prompt,
        }
        self.turns.append(turn)
        self.calls += 1
        plan = {"thought": f"{turn['name']} thinks.", "urge": 10, "speech": f"{turn['name']} speaks.",
                "asks": [], "ready_to_vote": False, "notes": ""}
        plan.update(self.script(turn, self.turns))
        self._planned = plan
        return {k: plan[k] for k in ("thought", "urge", "ready_to_vote", "notes")}


class ThreadedMock(MockBackend):
    """Exercises the thread-pool path."""

    max_concurrency = 4


class AlwaysFails:
    def __init__(self, max_concurrency: int = 1):
        self.max_concurrency = max_concurrency
        self.calls = 0

    def complete(self, system, prompt, schema):
        self.calls += 1
        raise BackendError("claude exited with status 1: not logged in")

    def stats(self):
        return {"calls": self.calls, "errors": self.calls, "seconds": 0.0}


class Recorder:
    def __init__(self):
        self.events: list[Event] = []
        self.busy_texts: list[str] = []

    def on_event(self, event):
        self.events.append(event)

    def busy(self, text):
        import contextlib

        self.busy_texts.append(text)
        return contextlib.nullcontext()


def play(seed: int, backend=None, **cfg) -> Game:
    game = Game(GameConfig(seed=seed, **cfg), backend or MockBackend(seed=seed))
    game.run()
    return game


def check_invariants(game: Game) -> None:
    events = game.events
    cfg = game.config

    # Shape: setup first, exactly one game_over, and it is last.
    assert events[0].kind == "setup"
    assert [e.kind for e in events].count("game_over") == 1
    last = events[-1]
    assert last.kind == "game_over" and last.phase == "end"

    # The winner is consistent with who is alive.
    wolves = sum(p.alive and p.is_wolf for p in game.players)
    others = sum(p.alive and not p.is_wolf for p in game.players)
    if game.winner is Team.VILLAGE:
        assert wolves == 0
    elif game.winner is Team.WOLVES:
        assert 0 < wolves and wolves >= others
    else:
        assert game.winner is None
        assert 0 < wolves < others
        assert game.day == cfg.max_days
    assert last.data["winner"] == (game.winner.value if game.winner else None)
    assert last.data["roles"] == {p.name: p.role.value for p in game.players}
    assert last.data["fates"] == {p.name: p.fate for p in game.players}

    # Dead players never act; deaths are consistent with state.
    dead: set[str] = set()
    for e in events:
        if e.kind in ACTING_KINDS:
            assert e.actor not in dead, f"{e.actor} acted after death: {e}"
        if e.kind == "death":
            assert e.target not in dead
            dead.add(e.target)
            assert e.data["role"] == game.by_name[e.target].role.value
    assert dead == {p.name for p in game.players if not p.alive}

    # Votes are legal.
    alive = {p.name for p in game.players}
    for e in events:
        if e.kind == "death":
            alive.discard(e.target)
        if e.kind == "vote":
            assert e.target != e.actor and e.target in alive and e.actor in alive

    # Wolves never target wolves; the victim is announced as the wolf decision.
    for e in events:
        if e.kind in ("wolf_chat", "wolf_pick", "wolf_decision"):
            assert not game.by_name[e.target].is_wolf
            assert e.visible_to and all(game.by_name[n].is_wolf for n in e.visible_to)

    # Doctor restrictions.
    for doc in (p for p in game.players if p.role is Role.DOCTOR):
        prot = [(e.day, e.target) for e in events if e.kind == "protect" and e.actor == doc.name]
        assert prot == doc.protected
        for (n1, t1), (n2, t2) in zip(prot, prot[1:]):
            assert not (n2 == n1 + 1 and t1 == t2), f"{doc.name} protected {t1} twice in a row"
        assert sum(t == doc.name for _, t in prot) <= 1
        assert doc.self_protect_used == any(t == doc.name for _, t in prot)

    # Sheriff restrictions.
    for sh in (p for p in game.players if p.role is Role.SHERIFF):
        inv = [e for e in events if e.kind == "investigate" and e.actor == sh.name]
        targets = [e.target for e in inv]
        assert sh.name not in targets
        assert len(targets) == len(set(targets))
        for e in inv:
            assert e.data["wolf"] == game.by_name[e.target].is_wolf
            assert e.visible_to == (sh.name,)
        assert list(sh.investigations) == targets

    # Night N belongs to day N; days never go backwards.
    days = [e.day for e in events]
    assert days == sorted(days)

    check_discussions(game)


def check_discussions(game: Game) -> None:
    """The urge-to-speak rules: the dice, the caps, and one ending per day."""
    cfg = game.config
    for day in range(1, game.day + 1):
        talk = [e for e in game.events if e.day == day and e.phase == "day"]
        turns = [e for e in talk if e.kind == "turn"]
        ends = [e for e in talk if e.kind == "discussion_end"]
        if not any(e.kind == "vote" for e in talk):
            assert not turns and not ends  # the game ended in the morning
            continue
        assert len(ends) == 1 and ends[0].data["reason"] in ("quiet", "ready", "cap")
        assert talk.index(ends[0]) < min(i for i, e in enumerate(talk) if e.kind == "vote")
        living = len({e.actor for e in talk if e.kind == "vote" and not e.data["runoff"]})
        assert len(turns) <= cfg.day_turns_per_player * living
        assert max(e.data["round"] for e in turns) <= cfg.max_rounds
        spoken: Counter = Counter()
        for i, e in enumerate(talk):
            if e.kind == "turn":
                urge, roll = e.data["urge"], e.data["roll"]
                assert 0 <= urge <= 10 and 1 <= roll <= 10
                assert spoken[e.actor] < cfg.max_speeches, f"{e.actor} had a turn after their last speech"
                if e.data["spoke"]:  # got the floor: the speech follows, unless the speech call failed
                    assert roll <= urge
                    nxt = talk[i + 1]
                    assert nxt.actor == e.actor and nxt.kind in ("speech", "fallback")
                    if nxt.kind == "speech":
                        assert nxt.data["reply_to"] == e.data["reply_to"] and nxt.data["round"] == e.data["round"]
                else:
                    assert urge == 0 or roll > urge
                assert e.visible_to == ()
            elif e.kind == "speech":
                assert talk[i - 1].kind == "turn" and talk[i - 1].data["spoke"]
                spoken[e.actor] += 1
                assert e.public and "urge" not in e.data and "roll" not in e.data
            elif e.kind == "ready":
                assert e.public and talk[i - 1].actor == e.actor
        assert max(spoken.values(), default=0) <= cfg.max_speeches
        # replies in a row never pass the chain limit
        run = longest = 0
        for e in turns:
            run = run + 1 if e.data["reply_to"] else 0
            longest = max(longest, run)
        assert longest <= cfg.max_replies_in_row


def check_no_leaks(game: Game) -> None:
    by_name = game.by_name
    wolf_chat = [e.text for e in game.events if e.kind == "wolf_chat" and e.text]
    thoughts = [(e.actor, e.text) for e in game.events if e.kind == "thought"]
    notes = [(e.actor, e.data["notes"]) for e in game.events if e.kind == "thought" and e.data.get("notes")]
    assert thoughts, "vacuous test: no thoughts"
    for name, kind, req in game.requests:
        player = by_name[name]
        text = req.system + "\n" + req.prompt
        if not player.is_wolf:
            assert "(wolf den)" not in text
            for chat in wolf_chat:
                assert chat not in text, f"{name} ({player.role.value}) saw wolf chat"
        for actor, thought in thoughts:
            if thought in text:  # only your own, handed back to you for the speech after the dice
                assert actor == name and kind == "speak", f"{name} saw a thought (by {actor})"
        for actor, note in notes:
            if actor != name:
                assert note not in text, f"{name} saw {actor}'s notes"
        if player.role is not Role.SHERIFF:
            assert "(private) Your investigation" not in text
        if player.role is not Role.DOCTOR:
            assert "(private) You protected" not in text
        # No dice and no urge scores, except a speaker's own urge when they get the floor.
        assert "rolled" not in req.prompt
        if kind != "speak":
            assert "/10" not in req.prompt
    # A speech is only ever written after the dice gave the floor: one speech call per turn that won it.
    turns = [e for e in game.events if e.kind == "turn"]
    speak_calls = [(name, req) for name, kind, req in game.requests if kind == "speak"]
    assert len(speak_calls) == sum(e.data["spoke"] for e in turns)
    assert all("speech" not in req.schema["properties"] for _, kind, req in game.requests if kind == "discuss")


# --------------------------------------------------------------------------------------------
# Whole games
# --------------------------------------------------------------------------------------------

def test_many_seeds_terminate_with_consistent_winner():
    winners = set()
    for seed in range(200):
        game = play(seed)
        check_invariants(game)
        winners.add(game.winner)
    assert {Team.VILLAGE, Team.WOLVES} <= winners


def test_win_condition_holds_at_end():
    for seed in range(50):
        game = play(seed)
        wolves = [p for p in game.players if p.alive and p.is_wolf]
        others = [p for p in game.players if p.alive and not p.is_wolf]
        if not wolves:
            assert game.winner is Team.VILLAGE
        elif len(wolves) >= len(others):
            assert game.winner is Team.WOLVES
        else:
            assert game.winner is None


def test_no_hidden_information_leaks():
    saw_investigation = saw_protection = saw_den = False
    for seed in range(60):
        game = play(seed, TaggedMock(seed=seed))
        check_invariants(game)
        check_no_leaks(game)
        for name, kind, req in game.requests:
            role = game.by_name[name].role
            saw_investigation |= role is Role.SHERIFF and "(private) Your investigation" in req.prompt
            saw_protection |= role is Role.DOCTOR and "(private) You protected" in req.prompt
            saw_den |= role is Role.WEREWOLF and "(wolf den)" in req.prompt
    # The checks above are not vacuous: the private lines do reach their owners.
    assert saw_investigation and saw_protection and saw_den


def test_prompts_only_use_visible_events():
    game = play(3, TaggedMock(seed=3))
    for name, kind, req in game.requests:
        log = req.prompt.split("## Game log", 1)[1].split("## Your notes", 1)[0]
        for e in game.events:
            line = prompts.log_line(e)
            if line and not e.visible(name) and len(line) > 25:
                assert line not in log


def test_dead_players_never_get_a_request(monkeypatch):
    original = Agent.build

    def build(self, task):
        assert self.player.alive, f"request built for dead {self.player.name}"
        return original(self, task)

    monkeypatch.setattr(Agent, "build", build)
    for seed in range(30):
        play(seed)


def test_fallbacks_with_flaky_backend():
    fallbacks = 0
    for seed in range(20):
        backend = MockBackend(seed=seed, error_rate=0.3)
        game = play(seed, backend)
        check_invariants(game)
        fallbacks += sum(e.kind == "fallback" for e in game.events)
        assert backend.stats()["errors"] > 0
    assert fallbacks > 0


def test_retry_prompt_mentions_rejection():
    seen = []

    class Picky(MockBackend):
        def complete(self, system, prompt, schema):
            seen.append(prompt)
            if len(seen) == 1:
                return {"thought": "", "target": "Nobody", "notes": ""}
            return super().complete(system, prompt, schema)

    game = Game(GameConfig(seed=1, players=5, wolves=1, doctors=0, sheriffs=0), Picky(seed=1))
    game.run()
    assert "IMPORTANT: your previous reply was rejected" in seen[1]
    assert "Nobody" in seen[1]
    assert "IMPORTANT" not in seen[2]


@pytest.mark.parametrize("workers", [1, 4])
def test_abort_after_repeated_backend_failures(workers):
    rec = Recorder()
    game = Game(GameConfig(seed=5), AlwaysFails(workers), observers=[rec])
    with pytest.raises(GameAborted) as info:
        game.run()
    assert "logged in" in str(info.value) and "not logged in" in str(info.value)
    assert game.events[-1].kind == "aborted"
    assert rec.events == game.events
    assert [e.kind for e in game.events].count("fallback") == 3
    assert "game_over" not in [e.kind for e in game.events]


class FatalFails(AlwaysFails):
    def complete(self, system, prompt, schema):
        self.calls += 1
        raise BackendError("Claude usage limit reached; wait for your plan's limit to reset", fatal=True)


@pytest.mark.parametrize("workers", [1, 4])
def test_fatal_backend_error_aborts_without_retrying(workers):
    backend = FatalFails(workers)
    game = Game(GameConfig(seed=5), backend)
    with pytest.raises(GameAborted, match="usage limit reached"):
        game.run()
    kinds = [e.kind for e in game.events]
    assert kinds[-1] == "aborted" and "fallback" not in kinds and "game_over" not in kinds
    # One failing call per request already in flight: no retries (sequential mode stops after exactly 1).
    assert backend.calls <= (1 if workers == 1 else 3)


def test_threaded_backend_games_are_valid():
    for seed in range(20):
        game = play(seed, ThreadedMock(seed=seed))
        check_invariants(game)


@pytest.mark.parametrize(
    "cfg",
    [
        dict(players=6),
        dict(players=9),
        dict(players=9, wolves=3),
        dict(players=5, wolves=1),
        dict(players=7, wolves=1),
        dict(players=6, doctors=0, sheriffs=0),
        dict(players=8, doctors=2, sheriffs=2),
        dict(players=7, max_rounds=1, max_speeches=1, max_days=2),
        dict(players=9, max_replies_in_row=0),
        dict(players=6, max_rounds=8, max_speeches=8, max_replies_in_row=5, day_turns_per_player=2),
    ],
)
def test_other_configs(cfg):
    for seed in range(30):
        game = play(seed, TaggedMock(seed=seed), **cfg)
        check_invariants(game)
        check_no_leaks(game)
        assert len(game.players) == cfg["players"]
        assert sum(p.is_wolf for p in game.players) == cfg.get("wolves", 2)


def test_single_wolf_picks_alone():
    game = play(2, players=5, wolves=1)
    kinds = [e.kind for e in game.events]
    assert "wolf_chat" not in kinds
    night1 = [e for e in game.events if e.day == 1 and e.phase == "night"]
    assert [e.kind for e in night1 if e.kind.startswith("wolf")] == ["wolf_pick", "wolf_decision"]


def test_determinism():
    def dump(seed):
        return [e.to_dict() for e in play(seed).events]

    for seed in (0, 7, 42):
        assert dump(seed) == dump(seed)
    assert dump(1) != dump(2)


def test_observers_see_every_event_and_busy_texts():
    rec = Recorder()
    game = Game(GameConfig(seed=11), MockBackend(seed=11), observers=[rec])
    game.run()
    assert rec.events == game.events
    assert "🐺 The wolves are plotting…" in rec.busy_texts
    assert "🗳️ The village is voting…" in rec.busy_texts
    # Busy texts are shown in the public view too, so they never reveal a role.
    for text in rec.busy_texts:
        for p in game.players:
            if p.name in text:
                assert "wolf" not in text.lower() and "Doctor" not in text and "Sheriff" not in text


def test_setup_event():
    game = play(4)
    setup = game.events[0]
    assert setup.visible_to == ()
    assert [d["name"] for d in setup.data["players"]] == [p.name for p in game.players]
    assert {d["role"] for d in setup.data["players"]} <= {r.value for r in Role}
    assert setup.data["config"]["seed"] == 4
    assert set(setup.data["players"][0]) == {"name", "role", "job", "archetype", "quirk", "color"}


def test_night_order():
    game = play(8)
    night = [e.kind for e in game.events if e.day == 1 and e.phase == "night" and e.kind != "thought"]
    assert night[0] == "phase"
    decision = night.index("wolf_decision")
    assert all(k.startswith("wolf") for k in night[1:decision])
    assert night[decision + 1:] == [k for k in night[decision + 1:] if k in ("protect", "investigate")]
    if "protect" in night and "investigate" in night:
        assert night.index("protect") < night.index("investigate")
    day1 = [e.kind for e in game.events if e.day == 1 and e.phase == "day"]
    assert day1[0] == "phase"
    assert day1[1] in ("saved", "announce")


# --------------------------------------------------------------------------------------------
# Tie and runoff (scripted votes on Day 1)
# --------------------------------------------------------------------------------------------

def scripted_votes(monkeypatch, runoff_decides: bool):
    """Day 1 of a no-Doctor game always has 6 players alive. Script a 3-3 tie between the first two."""
    original = Agent.execute

    def execute(self, task, req):
        game = self.game
        if game.day != 1 or task.kind not in ("vote", "runoff_vote") or len(task.targets) == 1:
            return original(self, task, req)
        living = [p.name for p in game.players if p.alive]
        a, b = living[0], living[1]
        me = self.player.name
        if me == a:
            target = b
        elif me == b:
            target = a
        else:
            rest = [n for n in living if n not in (a, b)]
            alternate = a if rest.index(me) % 2 == 0 else b
            target = a if (task.kind == "runoff_vote" and runoff_decides) else alternate
        return Decision(target=target, reason="scripted")

    monkeypatch.setattr(Agent, "execute", execute)


CFG_NO_DOCTOR = dict(players=7, wolves=2, doctors=0, sheriffs=1)


def day1(game, kind):
    return [e for e in game.events if e.day == 1 and e.phase == "day" and e.kind == kind]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_tie_then_runoff_eliminates(monkeypatch, seed):
    scripted_votes(monkeypatch, runoff_decides=True)
    game = play(seed, **CFG_NO_DOCTOR)
    check_invariants(game)
    first, runoff = day1(game, "vote_result")
    a, b = first.data["tied"]
    assert first.target is None and not first.data["runoff"] and first.data["tally"] == {a: 3, b: 3}
    assert sorted(e.actor for e in day1(game, "defense")) == sorted([a, b])
    runoff_votes = [e for e in day1(game, "vote") if e.data["runoff"]]
    assert len(runoff_votes) == 6 and all(e.target in (a, b) and e.target != e.actor for e in runoff_votes)
    assert any(e.data["auto"] for e in runoff_votes)  # the tied players had only one option
    assert runoff.data["runoff"] and runoff.target == a and runoff.data["tied"] == []
    assert [e.actor for e in day1(game, "last_words")] == [a]
    deaths = [e for e in day1(game, "death") if e.data["fate"] == "voted out on Day 1"]
    assert [e.target for e in deaths] == [a]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_tie_then_runoff_still_tied_eliminates_nobody(monkeypatch, seed):
    scripted_votes(monkeypatch, runoff_decides=False)
    game = play(seed, **CFG_NO_DOCTOR)
    check_invariants(game)
    first, runoff = day1(game, "vote_result")
    assert runoff.data["runoff"] and runoff.target is None
    assert sorted(runoff.data["tied"]) == sorted(first.data["tied"])
    assert "nobody is eliminated" in runoff.text.lower()
    assert not day1(game, "last_words")
    assert not [e for e in game.events if e.kind == "death" and e.data["fate"] == "voted out on Day 1"]


# --------------------------------------------------------------------------------------------
# Agent validation
# --------------------------------------------------------------------------------------------

def test_parse_reply_canonicalises_and_clips():
    task = Task(kind="wolf_chat", header="", instructions="", targets=["Alice", "Bram"], speech_words=4)
    dec = parse_reply(task, {"thought": " t ", "speech": '"one two three four five six seven"',
                             "target": "bram", "notes": "n"}, speaker="Clara")
    assert dec.target == "Bram"
    assert dec.speech == "one two three four five six…"
    assert dec.thought == "t"


def test_parse_reply_rejections():
    speak = Task(kind="defense", header="", instructions="", speech_words=10)
    with pytest.raises(ValueError):
        parse_reply(speak, {"thought": "", "speech": "  ", "notes": ""})
    with pytest.raises(ValueError):
        parse_reply(speak, {"thought": "", "notes": ""})
    with pytest.raises(ValueError, match="empty"):
        parse_reply(SPEAK, {"speech": '""', "asks": []})
    vote = Task(kind="vote", header="", instructions="", targets=["Alice", "Bram"], reason_words=5)
    with pytest.raises(ValueError):
        parse_reply(vote, {"thought": "", "target": "Zed", "reason": "x", "notes": ""})
    with pytest.raises(ValueError):
        parse_reply(vote, {"thought": "", "target": "Alice", "notes": ""})
    with pytest.raises(ValueError):
        parse_reply(vote, ["not", "a", "dict"])
    assert parse_reply(SPEAK, {"speech": "Clara: Hello there."}, speaker="Clara").speech == "Hello there."


DISCUSS = Task(kind="discuss", header="", instructions="", urge=True)
SPEAK = Task(kind="speak", header="", instructions="", speech_words=10, asks=["Alice", "Bram"], private_fields=False)


@pytest.mark.parametrize("urge, expected", [(7, 7), (7.5, 8), (6.5, 7), (0.4, 0), (11, 10), (-2, 0), ("7", 7),
                                            ("7/10", 7), (" 3 ", 3)])
def test_parse_reply_urge_is_rounded_and_clamped(urge, expected):
    assert parse_reply(DISCUSS, {"thought": "t", "urge": urge, "ready_to_vote": False, "notes": "n"}).urge == expected


@pytest.mark.parametrize("urge", [None, True, "loud", float("nan"), [7]])
def test_parse_reply_rejects_bad_urge(urge):
    with pytest.raises(ValueError, match="urge"):
        parse_reply(DISCUSS, {"urge": urge})


def test_parse_reply_ready():
    assert parse_reply(DISCUSS, {"urge": 9, "ready_to_vote": "true"}).ready_to_vote is True
    assert parse_reply(DISCUSS, {"urge": 9}).ready_to_vote is None  # missing: readiness doesn't change
    assert parse_reply(DISCUSS, {"urge": 9, "speech": "ignored"}).speech == ""  # no speech before the dice
    with pytest.raises(ValueError, match="ready_to_vote"):
        parse_reply(DISCUSS, {"urge": 9, "ready_to_vote": "maybe"})


def test_parse_reply_asks():
    dec = parse_reply(SPEAK, {"speech": "Bram? Alice?", "asks": ["bram", "Zed", "Clara", "Bram", '"Alice"']},
                      speaker="Clara")
    assert dec.asks == ["Bram", "Alice"]  # canonical, in order, unknown names and repeats dropped
    assert parse_reply(SPEAK, {"speech": "Hi.", "asks": "alice"}).asks == ["Alice"]
    assert parse_reply(SPEAK, {"speech": "Hi."}).asks == []
    with pytest.raises(ValueError, match="asks"):
        parse_reply(SPEAK, {"speech": "Hi.", "asks": {"Bram": 1}})


def test_notes_are_clipped():
    task = Task(kind="protect", header="", instructions="", targets=["Alice", "Bram"])
    dec = parse_reply(task, {"thought": "", "target": "Alice", "notes": "word " * 400})
    assert len(dec.notes.split()) <= 151


def test_only_choice_is_automatic():
    game = Game(GameConfig(seed=1), AlwaysFails())
    agent = next(iter(game.agents.values()))
    task = Task(kind="runoff_vote", header="", instructions="", targets=["Bram"], reason_words=5)
    dec = agent.execute(task, agent.build(task))
    assert dec.auto and dec.target == "Bram" and not dec.fallback
    assert game.backend.calls == 0


def test_fallback_marks_backend_errors_only_when_every_attempt_failed():
    game = Game(GameConfig(seed=1, retries=2), AlwaysFails())
    agent = next(iter(game.agents.values()))
    task = Task(kind="vote", header="", instructions="", targets=["Alice", "Bram"], reason_words=5)
    dec = agent.execute(task, agent.build(task))
    assert dec.fallback and dec.backend_error and dec.target in ("Alice", "Bram") and "not logged in" in dec.error
    assert game.backend.calls == 3

    class HalfBroken(MockBackend):
        def complete(self, system, prompt, schema):
            if self.calls % 2 == 0:
                self.calls += 1
                raise BackendError("boom")
            self.calls += 1
            return {"thought": "", "target": "Nobody", "reason": "x", "notes": ""}

    game = Game(GameConfig(seed=1, retries=2), HalfBroken(seed=1))
    agent = next(iter(game.agents.values()))
    dec = agent.execute(task, agent.build(task))
    assert dec.fallback and not dec.backend_error


# --------------------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------------------

def test_discuss_schema_shape():
    game = Game(GameConfig(seed=1), MockBackend(seed=1))
    game.day, game.phase, game.discussion.round = 1, "day", 1
    me = game.players[0]
    schema = prompts.schema_for(prompts.discuss_task(game, me))
    props = schema["properties"]
    assert list(props) == ["thought", "urge", "ready_to_vote", "notes"]  # no speech before the dice
    assert props["urge"]["type"] == "integer" and props["ready_to_vote"]["type"] == "boolean"
    assert schema["required"] == list(props)
    speak = prompts.schema_for(prompts.speak_task(game, me, 7, "my plan", []))
    assert list(speak["properties"]) == ["speech", "asks"] and speak["required"] == ["speech", "asks"]
    assert speak["properties"]["asks"]["items"]["enum"] == [p.name for p in game.players if p is not me]


def test_schema_shape():
    task = Task(kind="vote", header="", instructions="", targets=["Alice", "Bram"], reason_words=20)
    schema = prompts.schema_for(task)
    assert list(schema["properties"]) == ["thought", "target", "reason", "notes"]
    assert schema["required"] == list(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["target"]["enum"] == ["Alice", "Bram"]
    assert all(p.get("description") for p in schema["properties"].values())
    chat = Task(kind="wolf_chat", header="", instructions="", targets=["Alice"], speech_words=50)
    assert list(prompts.schema_for(chat)["properties"]) == ["thought", "speech", "target", "notes"]


def test_log_line_hides_private_kinds():
    for kind in ("setup", "thought", "fallback", "saved", "game_over"):
        assert prompts.log_line(Event(kind=kind, day=1, phase="day", text="secret")) is None
    chat = Event(kind="wolf_chat", day=1, phase="night", text="Kill her.", actor="Bram", target="Clara")
    assert prompts.log_line(chat) == '(wolf den) Bram: "Kill her." [proposes Clara]'
    assert prompts.log_line(Event(kind="protect", day=1, phase="night", actor="D", target="X")) == \
        "(private) You protected X."
    assert prompts.log_line(Event(kind="investigate", day=1, phase="night", actor="S", target="X",
                                  data={"wolf": True})) == "(private) Your investigation: X is A WOLF."
    assert prompts.log_line(Event(kind="phase", day=2, phase="night", text="...")) == "=== Night 2 ==="


def test_turn_prompt_sections_in_order():
    game = play(6)
    for name, kind, req in game.requests:
        heads = [req.prompt.index(h) for h in
                 ("## Players", "## Your private knowledge", "## Game log", "## Your notes from last turn",
                  "## Your task")]
        assert req.prompt.startswith("# ") and heads == sorted(heads)


def test_system_prompt_contents():
    game = Game(GameConfig(seed=9), MockBackend(seed=9))
    for p in game.players:
        text = prompts.system_prompt(game, p)
        assert text.startswith(f"You are {p.name}, the village {p.persona.job}.")
        assert game.config.role_summary() in text
        assert p.persona.quirk in text and p.persona.archetype in text
        assert "anyone may lie" in text
        others = [q.name for q in game.players if q.is_wolf and q is not p]
        if p.is_wolf:
            assert all(n in text for n in others) and "NEVER reveal" in text
        else:
            assert "wolf den)" not in text
            # A villager's system prompt must not hint at who the wolves are.
            for w in (q for q in game.players if q.is_wolf):
                assert f"is you and {w.name}" not in text


# --------------------------------------------------------------------------------------------
# The stakes section and Sheriff hints
# --------------------------------------------------------------------------------------------

def _day_game(wolves_alive: int, others_alive: int, doctor_alive: bool = True) -> Game:
    """A fresh 9-player game with players killed off to the given counts, set to the day phase."""
    game = Game(GameConfig(seed=1, players=9), MockBackend(seed=1))
    wolves = [p for p in game.players if p.is_wolf]
    # Kill the Doctor first when asked, so the survivors don't include one.
    others = sorted((p for p in game.players if not p.is_wolf), key=lambda p: (p.role is Role.DOCTOR) != doctor_alive)
    for p in wolves[wolves_alive:] + others[others_alive:]:
        p.alive = False
    assert any(p.alive and p.role is Role.DOCTOR for p in game.players) == doctor_alive
    game.phase, game.day = "day", 3
    return game


def test_stakes_decisive_vote():
    text = prompts._stakes_section(_day_game(2, 3))
    assert "2 werewolves are still alive among the 5 living players" in text
    assert "win on the spot" in text and "there is no later" in text


@pytest.mark.parametrize("doctor_alive, phrase", [(True, "unless the Doctor protects"), (False, "no Doctor is left")])
def test_stakes_lose_tonight(doctor_alive, phrase):
    text = prompts._stakes_section(_day_game(2, 4, doctor_alive=doctor_alive))
    assert "will win tonight" in text and phrase in text and "there is no later" in text


@pytest.mark.parametrize("others, spare", [(5, "1 more day "), (7, "2 more days")])
def test_stakes_days_to_spare(others, spare):
    text = prompts._stakes_section(_day_game(2, others))
    assert f"can afford {spare}" in text and "there is no later" not in text


def test_stakes_only_on_day_turns():
    game = play(3)
    for name, kind, req in game.requests:
        day_turn = kind in {"discuss", "speak", "vote", "runoff_vote", "defense", "last_words"}
        assert ("## The stakes" in req.prompt) == day_turn, (name, kind)


def test_sheriff_is_told_to_vote_for_found_wolf():
    game = _day_game(2, 7)
    sheriff = next(p for p in game.players if p.role is Role.SHERIFF)
    wolf = next(p for p in game.players if p.is_wolf and p.alive)
    villager = next(p for p in game.players if p.role is Role.VILLAGER and p.alive)
    sheriff.investigations = {wolf.name: True, villager.name: False}
    text = prompts._knowledge_section(game, sheriff, prompts.vote_task(game, sheriff, [wolf.name, villager.name]))
    assert f"Werewolves you found who are still alive: {wolf.name}" in text
    assert f"Living players you cleared (NOT wolves): {villager.name}" in text
    assert "Never vote for anyone else while a wolf you found is alive" in prompts.system_prompt(game, sheriff)


# --------------------------------------------------------------------------------------------
# The discussion: dice, reply queue, endings and caps (scripted discussion turns)
# --------------------------------------------------------------------------------------------

def talk(game, day=1, kind="turn", rnd=None):
    return [e for e in game.events if e.day == day and e.phase == "day" and e.kind == kind
            and (rnd is None or e.data.get("round") == rnd)]


def alive_on(game, day=1) -> int:
    """How many players were alive for that day's discussion (everyone alive casts a first-round vote)."""
    return len([e for e in talk(game, day, "vote") if not e.data["runoff"]])


def ending(game, day=1):
    (end,) = talk(game, day, "discussion_end")
    return end.data["reason"], end.data["round"]


def scripted(script=None, seed=1, **cfg) -> Game:
    game = play(seed, Scripted(script, seed=seed), **cfg)
    check_invariants(game)
    return game


def test_dice_urge_0_never_speaks_and_10_always_does():
    game = scripted(lambda t, ts: {"urge": 0 if t["name"] < "M" else 10, "ready_to_vote": t["round"] >= 2},
                    players=9, max_days=3)
    turns = [e for e in game.events if e.kind == "turn"]
    assert {e.data["spoke"] for e in turns if e.data["urge"] == 0} == {False}
    assert {e.data["spoke"] for e in turns if e.data["urge"] == 10} == {True}
    assert len({e.data["urge"] for e in turns}) == 2


def test_dice_follow_the_urge():
    game = scripted(lambda t, ts: {"urge": 5}, players=9, max_days=10, seed=3)
    turns = [e for e in game.events if e.kind == "turn"]
    assert len(turns) > 100
    assert all(e.data["spoke"] == (e.data["roll"] <= 5) for e in turns)
    assert 0.35 < sum(e.data["spoke"] for e in turns) / len(turns) < 0.65


def test_quiet_round_ends_the_talk():
    game = scripted(lambda t, ts: {"urge": 0, "speech": ""}, players=7)
    assert ending(game) == ("quiet", 1)
    assert len(talk(game)) == alive_on(game) and not talk(game, kind="speech")
    assert "A quiet moment falls" in talk(game, kind="discussion_end")[0].text


def test_ready_majority_waits_for_round_1_to_finish():
    game = scripted(lambda t, ts: {"ready_to_vote": True}, players=9)
    alive = alive_on(game)
    assert ending(game) == ("ready", 1)
    assert len(talk(game)) == alive  # everyone had their turn, though a majority was ready halfway through
    assert talk(game, kind="discussion_end")[0].text == "Most of the village is ready to vote."
    counts = [e.data["count"] for e in talk(game, kind="ready")]
    assert counts == list(range(1, alive + 1)) and all(e.data["living"] == alive for e in talk(game, kind="ready"))


def test_ready_majority_ends_round_2_at_once():
    game = scripted(lambda t, ts: {"ready_to_vote": t["round"] >= 2}, players=9)
    alive = alive_on(game)
    assert ending(game) == ("ready", 2)
    assert len(talk(game, rnd=1)) == alive and len(talk(game, rnd=2)) == alive // 2 + 1  # just over half


def test_players_can_change_their_mind_about_voting():
    flips = iter([True, False] * 50)
    game = scripted(lambda t, ts: {"ready_to_vote": next(flips)}, players=7, max_days=1)
    states = [e.data["ready"] for e in talk(game, kind="ready")]
    assert True in states and False in states


def test_round_cap_and_speech_cap():
    game = scripted(players=7)
    assert ending(game) == ("cap", 4)
    assert len(talk(game)) == 4 * alive_on(game)
    assert set(Counter(e.actor for e in talk(game, kind="speech")).values()) == {4}
    assert talk(game, kind="discussion_end")[0].text == "The sun is setting. Time to vote."


def test_speech_cap_ends_the_talk_when_everyone_is_done():
    game = scripted(players=7, max_speeches=1)
    assert ending(game) == ("cap", 1) and len(talk(game)) == alive_on(game)


def test_silent_turns_dont_use_up_speeches():
    def only_first_speaks_in_round_1(t, ts):
        return {"urge": 10 if t["round"] > 1 or t is ts[0] else 0}

    game = scripted(only_first_speaks_in_round_1, players=7, max_speeches=1, max_days=1)
    first = talk(game)[0].actor
    round2 = talk(game, rnd=2)
    # Everyone who was silent in round 1 still had their one speech; the first speaker was done for the day.
    assert len(round2) == alive_on(game) - 1 and first not in {e.actor for e in round2}
    assert all(e.data["spoke"] for e in round2)
    assert ending(game) == ("cap", 2)


def test_turn_cap_stops_a_runaway_discussion():
    def everyone_asks(t, ts):
        return {"asks": [n for n in t["legal"]][:2]}

    game = scripted(everyone_asks, players=7, max_speeches=50, max_rounds=50, max_replies_in_row=50,
                    day_turns_per_player=2)
    assert ending(game) == ("cap", talk(game)[-1].data["round"])
    assert len(talk(game)) == 2 * alive_on(game)


def test_asked_player_answers_next():
    def first_speaker_asks(t, ts):
        firsts = [x for x in ts if (x["day"], x["round"]) == (t["day"], t["round"])]
        return {"asks": [t["legal"][-1]]} if len(firsts) == 1 else {}

    game = scripted(first_speaker_asks, players=9)
    for rnd in (1, 2):
        turns = talk(game, rnd=rnd)
        first, second = turns[0], turns[1]
        asked = talk(game, kind="speech", rnd=rnd)[0].data["asks"]
        assert len(asked) == 1 and second.actor == asked[0] and second.data["reply_to"] == first.actor
        # The reply used up their turn: one turn each, so the round is the usual length.
        assert len(turns) == alive_on(game) and len({e.actor for e in turns}) == alive_on(game)
        assert talk(game, kind="speech", rnd=rnd)[1].data["reply_to"] == first.actor


def test_asked_player_who_already_spoke_gets_an_extra_turn():
    def last_asks_first(t, ts):
        this_round = [x for x in ts if (x["day"], x["round"]) == (t["day"], t["round"])]
        names = list(dict.fromkeys(x["name"] for x in this_round))
        if t["round"] == 1 and len(this_round) == len(names) == t["alive"]:
            return {"asks": [names[0]]}  # the last speaker of round 1 asks the first
        return {}

    game = scripted(last_asks_first, players=9)
    turns = talk(game, rnd=1)
    assert len(turns) == alive_on(game) + 1 and turns[-1].actor == turns[0].actor
    assert turns[-1].data["reply_to"] == turns[-2].actor
    assert Counter(e.actor for e in turns)[turns[0].actor] == 2


def test_reply_chains_stop_after_the_limit():
    def ping_pong(t, ts):  # always ask whoever spoke just before
        prev = [x["name"] for x in ts[:-1] if x["name"] != t["name"]]
        return {"asks": prev[-1:]}

    game = scripted(ping_pong, players=9, max_speeches=20, max_rounds=2, max_days=1)
    flags = [bool(e.data["reply_to"]) for e in talk(game)]
    runs = [len(r) for r in "".join("R" if f else "." for f in flags).split(".") if r]
    assert max(runs) == 3  # check_invariants already proved it never goes past 3
    # The round carried on after each chain: every player still had a normal turn in round 1.
    voters = {e.actor for e in talk(game, kind="vote")}
    assert {e.actor for e in talk(game, rnd=1) if not e.data["reply_to"]} == voters


def test_replies_are_answered_in_the_order_named():
    def first_asks_two(t, ts):
        if len(ts) == 1:
            return {"asks": t["legal"][:2]}
        return {}

    game = scripted(first_asks_two, players=9, max_days=1)
    turns = talk(game, rnd=1)
    asked = talk(game, kind="speech", rnd=1)[0].data["asks"]
    assert [e.actor for e in turns[1:3]] == asked and all(e.data["reply_to"] == turns[0].actor for e in turns[1:3])


def test_capped_player_is_skipped_even_when_asked():
    def ask_first_speaker(t, ts):
        first = ts[0]["name"]
        return {"asks": [first]} if t["name"] != first else {}

    game = scripted(ask_first_speaker, players=9, max_speeches=2, max_days=1)
    first = talk(game)[0].actor
    assert Counter(e.actor for e in talk(game, kind="speech"))[first] == 2
    assert Counter(e.actor for e in talk(game))[first] == 2  # no calls once they were done for the day


def test_direct_question_and_status_reach_the_prompt():
    def first_asks(t, ts):
        return {"asks": [t["legal"][0]]} if len(ts) == 1 else {"ready_to_vote": len(ts) == 3}

    backend = Scripted(first_asks, seed=2)
    game = play(2, backend, players=9, max_days=1)
    first, second, third = backend.turns[:3]
    assert "asked you directly" not in first["prompt"]
    assert f"{first['name']} asked you directly, so you get to answer now, out of turn." in second["prompt"]
    assert "Spoken today: nobody yet." in first["prompt"] and "Silent so far: " in first["prompt"]
    assert f"Spoken today: {first['name']} (1)." in second["prompt"]
    alive = alive_on(game)
    assert f"Nobody is ready to vote yet; the vote starts once {alive // 2 + 1} of {alive} are ready." in third["prompt"]
    fourth = backend.turns[3]["prompt"]
    assert f"Ready to vote: {third['name']} (1 of {alive}" in fourth
    assert f"{third['name']} is ready to vote. (1 of {alive} are ready.)" in fourth  # also in the game log
    assert "discussion round 1. The discussion ends after at most 4 rounds" in first["prompt"]


def test_hesitation_is_told_only_to_the_player():
    def one_talker(t, ts):  # the first speaker keeps the talk going; everyone else has urge 1 and usually misses
        return {"urge": 10 if t["name"] == ts[0]["name"] else 1}

    game = scripted(one_talker, players=7, max_days=2, seed=4)
    hesitated = {e.actor for e in game.events if e.kind == "turn" and e.data["urge"] == 1 and not e.data["spoke"]}
    told = {name for name, kind, req in game.requests
            if "you wanted to speak, but you didn't get the floor" in req.prompt}
    assert told and told <= hesitated


def test_fallback_turn_is_silent_and_keeps_readiness():
    class Broken(Scripted):
        def complete(self, system, prompt, schema):
            if "urge" in schema["properties"] and "round 2" in prompt and "You are " + self.victim in system:
                self.calls += 1
                raise BackendError("boom")
            return super().complete(system, prompt, schema)

    backend = Broken(lambda t, ts: {"urge": 10, "ready_to_vote": t["round"] == 1 and t["name"] == backend.victim})
    probe = Game(GameConfig(seed=1, players=9), MockBackend(seed=1))
    backend.victim = probe.players[0].name
    game = play(1, backend, players=9, max_days=1)
    check_invariants(game)
    victim = [e for e in talk(game, rnd=2) if e.actor == backend.victim]
    assert victim, "the chosen player must be alive on Day 1 for this seed"
    assert not victim[0].data["spoke"] and victim[0].data["urge"] == 0
    assert victim[0].data["ready_to_vote"] is True  # unchanged by the failed turn
    assert any(e.kind == "fallback" and e.actor == backend.victim for e in game.events)


def test_speech_is_written_only_after_the_dice():
    urges = itertools.cycle([0, 3, 7, 10])
    backend = Scripted(lambda t, ts: {"urge": next(urges), "thought": f"PLAN-{len(ts)} for {t['name']}"}, seed=5)
    game = play(5, backend, players=9, max_days=2)
    check_invariants(game)
    turns = [e for e in game.events if e.kind == "turn"]
    assert len(backend.speak_prompts) == sum(e.data["spoke"] for e in turns) > 0
    # Each speech call comes right after its turn won the floor, and hands the player their own plan back.
    calls = [(name, kind, req) for name, kind, req in game.requests if kind in ("discuss", "speak")]
    for (name, kind, req), (name2, kind2, req2) in zip(calls, calls[1:]):
        if kind2 == "speak":
            assert kind == "discuss" and name == name2
            plan = re.search(r"PLAN-\d+ for \w+", req.prompt) or re.search(r"PLAN-\d+ for \w+", req2.prompt)
            assert plan and f'What you were thinking a moment ago: "{plan.group(0)}"' in req2.prompt
            assert "you got the floor" in req2.prompt
    assert not any(e.data["urge"] == 0 and e.data["spoke"] for e in turns)


def test_speech_call_names_who_asked():
    def first_asks(t, ts):
        return {"asks": [t["legal"][0]]} if len(ts) == 1 else {}

    backend = Scripted(first_asks, seed=2)
    play(2, backend, players=9, max_days=1)
    asker = backend.turns[0]["name"]
    assert f"{asker} asked you directly, and you are answering out of turn: answer them." in backend.speak_prompts[1]
    assert "asked you directly" not in backend.speak_prompts[0]


def test_failed_speech_call_says_nothing():
    class SpeechFails(Scripted):
        def complete(self, system, prompt, schema):
            if "asks" in schema["properties"]:
                self.calls += 1
                raise BackendError("boom")
            return super().complete(system, prompt, schema)

    game = play(1, SpeechFails(lambda t, ts: {"urge": 10 if len(ts) == 1 else 0}, seed=1), players=9, max_days=1)
    check_invariants(game)
    first = talk(game)[0]
    assert first.data["spoke"]  # won the floor...
    assert not talk(game, kind="speech")  # ...but nothing was said
    fallback = next(e for e in game.events if e.kind == "fallback")
    assert fallback.actor == first.actor and "speech" in fallback.text and "They say nothing." in fallback.text
    assert ending(game) == ("quiet", 1)


def test_opening_nudge_only_until_someone_speaks():
    backend = Scripted(lambda t, ts: {"urge": 0 if len(ts) < 3 else 10, "speech": "" if len(ts) < 3 else "Hi."}, seed=2)
    play(2, backend, players=9, max_days=1)
    nudge = "Nobody has spoken yet today, and someone has to open the discussion."
    assert all(nudge in t["prompt"] for t in backend.turns[:3])  # silent turns don't count as speaking
    assert not any(nudge in t["prompt"] for t in backend.turns[4:] if t["day"] == 1)


def test_thoughts_are_asked_for_in_full():
    game = Game(GameConfig(seed=1), MockBackend(seed=1))
    task = Task(kind="protect", header="", instructions="", targets=["Alice"])
    assert "inner monologue" in prompts.schema_for(task)["properties"]["thought"]["description"]
    me = game.players[0]
    assert f'"thought" is your inner voice as {me.name}' in prompts.system_prompt(game, me)
    # The 5.5 models' safeguards refuse requests that read like extracting the model's own reasoning.
    for name, kind, req in play(3).requests:
        text = req.system + req.prompt + json.dumps(req.schema)
        assert "private reasoning" not in text and "reasoning a moment ago" not in text, kind
