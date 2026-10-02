"""Display, logger and CLI tests on a hand-built event list (no engine or AI needed)."""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from wolf import cli, display as display_mod, logger as logger_mod
from wolf.config import GameConfig
from wolf.display import Display, fmt_duration
from wolf.logger import GameLogger, load_game, write_markdown
from wolf.state import SPECTATOR, Event

ROOT = Path(__file__).resolve().parents[1]
WOLVES = ("Alice", "Bram")

PLAYERS = [
    {"name": "Alice", "role": "Werewolf", "job": "baker", "archetype": "Loudmouth",
     "quirk": "keeps bringing up food", "color": "cyan"},
    {"name": "Bram", "role": "Werewolf", "job": "miller", "archetype": "Joker",
     "quirk": "gives other players nicknames", "color": "magenta"},
    {"name": "Clara", "role": "Sheriff", "job": "weaver", "archetype": "Detective",
     "quirk": "always ends with a question", "color": "yellow"},
    {"name": "Dmitri", "role": "Doctor", "job": "herbalist", "archetype": "Village Elder",
     "quirk": "quotes made-up old village sayings", "color": "green"},
    {"name": "Elena", "role": "Villager", "job": "fisher", "archetype": "Paranoid",
     "quirk": "distrusts anyone who stays quiet", "color": "bright_blue"},
    {"name": "Finn", "role": "Villager", "job": "potter", "archetype": "Quiet One",
     "quirk": "changes their mind easily", "color": "orange1"},
]
CONFIG = GameConfig(players=6, wolves=2, seed=42).to_dict()


def ev(kind, day, phase, text="", actor=None, target=None, visible_to=None, **data):
    return Event(kind=kind, day=day, phase=phase, text=text, actor=actor, target=target,
                 visible_to=visible_to, data=data)


def thought(day, phase, actor, text, task):
    return ev("thought", day, phase, text, actor=actor, visible_to=SPECTATOR, task=task,
              notes=f"NOTES-{actor} remember this")


def turn(rnd, actor, urge, roll, spoke, reply_to=None, ready=False):
    return ev("turn", 1, "day", actor=actor, visible_to=SPECTATOR, urge=urge, roll=roll, spoke=spoke,
              ready_to_vote=ready, round=rnd, reply_to=reply_to)


def make_events() -> list[Event]:
    n, d = "night", "day"
    return [
        ev("setup", 0, "setup", visible_to=SPECTATOR, players=PLAYERS, config=CONFIG),
        ev("phase", 1, n, "Night 1 falls over the village."),
        thought(1, n, "Bram", "SECRET-THOUGHT Clara asks too many questions.", "wolf_chat"),
        ev("wolf_chat", 1, n, "WOLFCHAT-ONE Clara is too sharp, we take her tonight.", actor="Bram",
           target="Clara", visible_to=WOLVES),
        thought(1, n, "Alice", "SECRET-THOUGHT Agree with Bram.", "wolf_chat"),
        ev("wolf_chat", 1, n, "WOLFCHAT-TWO Clara it is.", actor="Alice", target="Clara", visible_to=WOLVES),
        ev("wolf_pick", 1, n, actor="Alice", target="Clara", visible_to=WOLVES),
        ev("wolf_decision", 1, n, "The pack will attack Clara.", target="Clara", visible_to=WOLVES,
           random=False),
        thought(1, n, "Dmitri", "SECRET-THOUGHT Elena is loud, the wolves may want her.", "protect"),
        ev("protect", 1, n, actor="Dmitri", target="Elena", visible_to=("Dmitri",)),
        thought(1, n, "Clara", "SECRET-THOUGHT Bram smiles too much.", "investigate"),
        ev("investigate", 1, n, actor="Clara", target="Bram", visible_to=("Clara",), wolf=True),
        ev("phase", 1, d, "Day 1 dawns."),
        ev("announce", 1, d, "Clara was killed in the night.", target="Clara"),
        ev("death", 1, d, "Clara was a Sheriff.", target="Clara", role="Sheriff",
           fate="killed by the werewolves on Night 1"),
        thought(1, d, "Elena", "SECRET-THOUGHT Somebody here is lying.", "discuss"),
        turn(1, "Elena", urge=7, roll=3, spoke=True),
        ev("speech", 1, d, "PUBLIC-SPEECH Who attacked our Sheriff? Bram, I want answers, and I want them "
           "now, before the sun goes down and another one of us is dragged into the woods!",
           actor="Elena", round=1, reply_to=None, asks=["Bram"]),
        thought(1, d, "Bram", "SECRET-THOUGHT Deflect onto Elena.", "discuss"),
        turn(1, "Bram", urge=10, roll=8, spoke=True, reply_to="Elena"),
        ev("speech", 1, d, "I was at the mill all night, ask anyone.", actor="Bram", round=1, reply_to="Elena",
           asks=[]),
        thought(1, d, "Finn", "SECRET-THOUGHT Better to watch for now.", "discuss"),
        turn(1, "Finn", urge=2, roll=9, spoke=False, ready=True),
        ev("ready", 1, d, actor="Finn", ready=True, round=1, count=1, living=5),
        thought(1, d, "Dmitri", "SECRET-THOUGHT Nothing to add.", "discuss"),
        turn(2, "Dmitri", urge=0, roll=4, spoke=False),
        ev("discussion_end", 1, d, "A quiet moment falls over the village. Time to vote.", reason="quiet", round=2),
        ev("fallback", 1, d, "FALLBACK-TEXT Finn's reply was invalid twice; a random vote was cast.",
           actor="Finn", visible_to=SPECTATOR),
        thought(1, d, "Alice", "SECRET-THOUGHT Elena is an easy target.", "vote"),
        ev("vote", 1, d, actor="Alice", target="Elena", reason="Too eager to accuse.", runoff=False, auto=False),
        ev("vote", 1, d, actor="Bram", target="Elena", reason="Loud and pushy.", runoff=False, auto=False),
        ev("vote", 1, d, actor="Dmitri", target="Bram", reason="The mill story is thin.", runoff=False, auto=False),
        ev("vote", 1, d, actor="Elena", target="Bram", reason="Deflecting.", runoff=False, auto=False),
        ev("vote", 1, d, actor="Finn", target="Alice", reason="Random.", runoff=False, auto=False),
        ev("vote_result", 1, d, "Elena 2, Bram 2, Alice 1 · Tie between Elena and Bram.",
           tally={"Elena": 2, "Bram": 2, "Alice": 1}, tied=["Elena", "Bram"], runoff=False),
        ev("defense", 1, d, "I am just a fisher!", actor="Elena"),
        ev("defense", 1, d, "It's not me, I swear on my flour.", actor="Bram"),
        ev("vote", 1, d, actor="Alice", target="Elena", reason="Still pushy.", runoff=True, auto=False),
        ev("vote", 1, d, actor="Dmitri", target="Bram", reason="Still thin.", runoff=True, auto=False),
        ev("vote", 1, d, actor="Finn", target="Elena", reason="Coin flip.", runoff=True, auto=False),
        ev("vote_result", 1, d, "Elena 2, Bram 1 · Elena is eliminated.", target="Elena",
           tally={"Elena": 2, "Bram": 1}, tied=[], runoff=True),
        thought(1, d, "Elena", "SECRET-THOUGHT I was right about Bram.", "last_words"),
        ev("last_words", 1, d, "LAST-WORDS Bram is lying, mark my words.", actor="Elena"),
        ev("death", 1, d, "Elena was a Villager.", target="Elena", role="Villager", fate="voted out on Day 1"),
        ev("game_over", 1, "end", "The werewolves can no longer be outvoted. The wolves win!",
           winner="wolves", roles={p["name"]: p["role"] for p in PLAYERS},
           fates={"Alice": None, "Bram": None, "Clara": "killed by the werewolves on Night 1",
                  "Dmitri": None, "Elena": "voted out on Day 1", "Finn": None}),
    ]


def render(events, **kwargs) -> str:
    console = Console(record=True, width=100, file=io.StringIO())
    d = Display(console=console, **kwargs)
    for e in events:
        d.on_event(e)
    return console.export_text()


# ---------------------------------------------------------------------- display

def test_god_view_shows_secrets():
    out = render(make_events(), view="god")
    for needle in ("WOLFCHAT-ONE", "WOLFCHAT-TWO", "SECRET-THOUGHT", "wolf den", "🩺 private", "⭐ private",
                   "FALLBACK-TEXT", "PUBLIC-SPEECH", "LAST-WORDS", "Alice 🐺", "stays quiet",
                   "The pack will attack", "WOLF", "Clara was killed", "Defense:", "Last words:",
                   "THE WEREWOLVES WIN", "runoff", "Discussion · round 1", "Discussion · round 2",
                   "🙋 7/10 · rolled 3", "🙋 10/10 · ↩ answers Elena", "🤐 2/10 · rolled 9 · stays quiet",
                   "🤐 0/10 · stays quiet", "Finn is ready to vote", "(1 of 5 ready)", "A quiet moment falls"):
        assert needle in out, needle
    assert "NOTES-" not in out  # notes only with show_notes
    # the thought is printed right before its action, and the score between them
    assert out.index("Somebody here is lying") < out.index("7/10") < out.index("PUBLIC-SPEECH")
    assert out.index("Better to watch") < out.index("2/10") < out.index("Finn is ready")


def test_public_view_hides_secrets():
    out = render(make_events(), view="public")
    for secret in ("WOLFCHAT", "SECRET-THOUGHT", "FALLBACK-TEXT", "wolf den", "private", "NOTES-",
                   "The pack will attack", "protects", "investigates", "/10", "rolled", "· stays quiet", "🤐", "🙋"):
        assert secret not in out, secret
    for public in ("PUBLIC-SPEECH", "LAST-WORDS", "Clara was killed", "Elena is eliminated",
                   "THE WEREWOLVES WIN", "Personality", "Night 1 falls", "↩ answers Elena",
                   "Finn is ready to vote", "Discussion · round 1", "A quiet moment falls"):
        assert public in out, public
    before_end = out.split("THE WEREWOLVES WIN")[0]
    assert "Alice 🐺" not in before_end and "Bram 🐺" not in before_end
    # the roster has no Role column in public view, but game_over reveals every role
    assert "Werewolf" in out.split("THE WEREWOLVES WIN")[1]


def test_show_notes():
    out = render(make_events(), view="god", show_notes=True)
    assert "NOTES-Bram remember this" in out


def test_old_logs_with_pass_events_still_render(tmp_path):
    # Logs from before the urge-to-speak discussion: fixed rounds, and "pass" for silence.
    old_config = dict(CONFIG, discussion_rounds=2)
    for key in ("max_rounds", "max_speeches", "max_replies_in_row", "day_turns_per_player"):
        old_config.pop(key)
    events = [
        ev("setup", 0, "setup", visible_to=SPECTATOR, players=PLAYERS, config=old_config),
        ev("phase", 1, "day", "Day 1 dawns."),
        thought(1, "day", "Finn", "SECRET-THOUGHT Better to watch for now.", "discuss"),
        ev("pass", 1, "day", actor="Finn", round=1),
        ev("speech", 1, "day", "OLD-SPEECH Hello.", actor="Bram", round=2),
    ]
    out = render(events, view="god")
    assert "stays silent" in out and "OLD-SPEECH" in out and "Discussion · round 2" in out
    assert "2 Werewolves" in out  # the role summary ignores the old config key
    write_markdown(META, events, tmp_path / "old.md")
    md = (tmp_path / "old.md").read_text(encoding="utf-8")
    assert "*Finn stays silent.*" in md and "OLD-SPEECH" in md


def test_saved_and_aborted():
    events = make_events()[:2] + [
        ev("saved", 1, "day", target="Elena", visible_to=SPECTATOR),
        ev("announce", 1, "day", "Nobody died last night."),
        ev("aborted", 1, "end", "Too many backend errors in a row."),
    ]
    god = render(events, view="god")
    assert "✨" in god and "saved them" in god
    assert "Game aborted" in god and "Too many backend errors" in god
    public = render(events, view="public")
    assert "saved them" not in public and "Nobody died last night." in public


def test_unknown_and_malformed_events_do_not_crash():
    events = make_events()[:1] + [
        ev("mystery", 1, "day", "strange things happen", actor="Alice"),
        Event(kind="vote", day=1, phase="day", actor="Ghost", target=None, data=None),
        Event(kind="vote_result", day=1, phase="day", data={"tally": {"Ghost": "many"}}),
        Event(kind="death", day=1, phase="day", target="Nobody", data={"role": "Jester"}),
    ]
    out = render(events, view="god")
    assert "strange things happen" in out and "mystery" in out


def test_bad_player_color_falls_back():
    events = make_events()
    setup = events[0]
    players = [dict(p) for p in PLAYERS]
    players[0]["color"] = "not-a-color"
    events[0] = ev("setup", 0, "setup", visible_to=SPECTATOR, players=players, config=setup.data["config"])
    out = render(events, view="god")
    assert "display error" not in out and "WOLFCHAT-TWO" in out


def test_no_setup_still_renders():
    out = render(make_events()[1:], view="god")
    assert "WOLFCHAT-ONE" in out and "PUBLIC-SPEECH" in out


def test_delay_sleeps_after_talk_events(monkeypatch):
    naps = []
    monkeypatch.setattr(display_mod.time, "sleep", lambda s: naps.append(s))
    events = make_events()
    render(events, view="god", delay=0.25)
    talk = [e for e in events if e.kind in display_mod.TALK_KINDS]
    assert naps == [0.25] * len(talk)
    naps.clear()
    render(events, view="public", delay=0.25)
    assert len(naps) == len([e for e in talk if e.visible_to is None])


def test_pause_waits_before_every_phase(monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda *a: prompts.append(a) or "")
    render(make_events(), view="god", pause=True)
    assert len(prompts) == 2  # Night 1 and Day 1


def test_pause_stops_on_eof(monkeypatch):
    def eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    out = render(make_events(), view="god", pause=True)
    assert "THE WEREWOLVES WIN" in out


def test_busy_and_summary():
    console = Console(record=True, width=100, file=io.StringIO())
    d = Display(console=console)
    with d.busy("Alice is thinking…"):
        with d.busy("nested"):
            pass
    d.summary({"calls": 97, "errors": 1, "seconds": 812.4, "input_tokens": 120000,
               "output_tokens": 34567, "cost_usd": 1.2345, "retries": 3})
    out = console.export_text()
    for needle in ("97 calls", "1 error", "13:32", "154,567 tokens", "$1.23",
                   "API-equivalent cost (covered by your Claude plan)", "retries: 3"):
        assert needle in out, needle
    d.summary({})  # nothing to show, no crash


def test_fmt_duration():
    assert fmt_duration(0) == "0:00"
    assert fmt_duration(812.4) == "13:32"
    assert fmt_duration(3725) == "1:02:05"


def test_narrow_console_renders():
    console = Console(record=True, width=60, file=io.StringIO())
    d = Display(console=console)
    for e in make_events():
        d.on_event(e)
    out = console.export_text()
    assert max(len(line) for line in out.splitlines()) <= 60
    assert "PUBLIC-SPEECH" in out


# ---------------------------------------------------------------------- logger

META = {"config": CONFIG, "backend": "mock", "model": None, "seed": 42, "started": "2026-09-30T12:00:00"}


def test_logger_round_trip(tmp_path):
    events = make_events()
    log = GameLogger(tmp_path / "games", META)
    assert log.path.parent == tmp_path / "games" and log.path.suffix == ".jsonl"
    for e in events[:-1]:
        log.on_event(e)
    assert not log.md_path.exists()
    log.on_event(events[-1])  # game_over writes the transcript
    assert log.md_path.exists() and log.md_path.stem == log.path.stem
    log.close({"calls": 38, "errors": 0, "seconds": 1.5})
    log.close({"calls": 999})  # idempotent
    log.on_event(events[0])  # ignored after close

    first = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])
    assert first["type"] == "meta" and first["seed"] == 42
    meta, loaded = load_game(log.path)
    assert loaded == events
    assert meta["stats"] == {"calls": 38, "errors": 0, "seconds": 1.5}
    assert {k: meta[k] for k in META} == META


def test_markdown_transcript(tmp_path):
    log = GameLogger(tmp_path, META)
    for e in make_events():
        log.on_event(e)
    log.close({"calls": 38})
    md = log.md_path.read_text(encoding="utf-8")
    for needle in ("# 🐺 Wolf", "| Player | Role", "🐺 Werewolf", "## 🌙 Night 1", "## ☀️ Day 1",
                   "Wolf den (private)", "WOLFCHAT-ONE", "Doctor (private)", "Sheriff (private)",
                   "💭", "SECRET-THOUGHT", "- **Alice** 🐺 → **Elena** 🌾", "Runoff vote",
                   "Elena is eliminated", "## 🏁 Result", "The wolves win!", "📊 Stats", "38 calls",
                   "**Elena** 🌾 *(Paranoid)* · 🙋 7/10 (rolled 3): PUBLIC-SPEECH",
                   "**Bram** 🐺 *(Joker)* · 🙋 10/10 · ↩ *answers Elena*: I was at the mill",
                   "*🤐 2/10 (rolled 9) · Finn stays quiet.*", "*✋ Finn is ready to vote (1 of 5 ready).*",
                   "Discussion · round 2", "**🔔 A quiet moment falls over the village. Time to vote.**",
                   "discussion of up to 4 rounds a day, 4 speeches per player"):
        assert needle in md, needle


def test_logger_name_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_mod, "_timestamp", lambda: "20260101-000000")
    a = GameLogger(tmp_path, META)
    b = GameLogger(tmp_path, META)
    assert a.path.name == "20260101-000000.jsonl"
    assert b.path.name == "20260101-000000-2.jsonl"
    a.close()
    b.close()


def test_load_game_skips_truncated_line(tmp_path):
    log = GameLogger(tmp_path, META)
    for e in make_events()[:5]:
        log.on_event(e)
    log.close()
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"type": "event", "kind": "spe')
    meta, events = load_game(log.path)
    assert len(events) == 5 and "stats" not in meta


def test_interrupted_game_still_gets_transcript(tmp_path):
    log = GameLogger(tmp_path, META)
    for e in make_events()[:10]:
        log.on_event(e)
    log.close()
    assert "ended early" in log.md_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- CLI

def test_play_defaults():
    a = cli.build_parser().parse_args(["play"])
    assert (a.players, a.wolves, a.no_doctor, a.no_sheriff, a.max_rounds, a.max_speeches, a.max_days, a.seed) == (
        9, 2, False, False, 4, 4, 10, None)
    assert (a.backend, a.model, a.effort, a.concurrency, a.timeout) == ("claude", "sonnet", None, 4, 180)
    assert (a.view, a.pause, a.delay, a.show_notes, a.log_dir, a.no_log) == ("god", False, 0, False, "games", False)


def test_replay_defaults():
    a = cli.build_parser().parse_args(["replay", "games/x.jsonl"])
    assert (a.file, a.view, a.pause, a.delay, a.md) == ("games/x.jsonl", "god", False, 0.4, False)


def test_bad_config_is_a_friendly_error(capsys):
    assert cli.main(["play", "--backend", "mock", "--players", "4", "--wolves", "2", "--no-log"]) == 2
    err = capsys.readouterr().err
    assert "too many wolves" in err and "Traceback" not in err


def test_missing_claude_binary(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["play", "--no-log", "--seed", "3"]) == 2
    assert "claude" in capsys.readouterr().err


def test_replay_command(tmp_path, capsys):
    log = GameLogger(tmp_path, META)
    for e in make_events():
        log.on_event(e)
    log.close({"calls": 38, "seconds": 75})
    capsys.readouterr()
    assert cli.main(["replay", str(log.path), "--delay", "0"]) == 0
    out = capsys.readouterr().out
    assert "PUBLIC-SPEECH" in out and "WOLFCHAT-ONE" in out and "38 calls" in out
    assert cli.main(["replay", str(log.path), "--delay", "0", "--view", "public"]) == 0
    assert "WOLFCHAT-ONE" not in capsys.readouterr().out

    log.md_path.unlink()
    assert cli.main(["replay", str(log.path), "--md"]) == 0
    assert log.md_path.exists()


def test_replay_missing_file(tmp_path, capsys):
    assert cli.main(["replay", str(tmp_path / "nope.jsonl")]) == 2
    assert "nope.jsonl" in capsys.readouterr().err


HAVE_ENGINE = (importlib.util.find_spec("wolf.engine") is not None
               and importlib.util.find_spec("wolf.backends.mock") is not None)
needs_engine = pytest.mark.skipif(not HAVE_ENGINE, reason="wolf.engine / wolf.backends.mock not written yet")


@needs_engine
def test_play_mock_in_process(tmp_path, capsys):
    assert cli.main(["play", "--backend", "mock", "--seed", "7", "--log-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Seed 7" in out and "Game over" in out and "calls" in out
    (log,) = tmp_path.glob("*.jsonl")
    meta, events = load_game(log)
    assert meta["seed"] == 7 and meta["backend"] == "mock" and meta["config"]["seed"] == 7
    assert "calls" in meta["stats"] and events[-1].kind == "game_over"


@needs_engine
def test_play_aborted_by_backend_errors(tmp_path, monkeypatch, capsys):
    from wolf.backends import mock
    from wolf.backends.base import BackendError

    class Broken:
        max_concurrency = 1

        def __init__(self, seed=None):
            pass

        def complete(self, system, prompt, schema):
            raise BackendError("the backend is down")

        def stats(self):
            return {"calls": 9, "errors": 9, "seconds": 0.0}

    monkeypatch.setattr(mock, "MockBackend", Broken)
    assert cli.main(["play", "--backend", "mock", "--seed", "2", "--log-dir", str(tmp_path)]) == 1
    assert "Game aborted" in capsys.readouterr().out
    (log,) = tmp_path.glob("*.jsonl")
    _, events = load_game(log)
    assert [e.kind for e in events].count("aborted") == 1 and events[-1].kind == "aborted"
    assert log.with_suffix(".md").exists()


@needs_engine
def test_play_ctrl_c_closes_the_log(tmp_path, monkeypatch, capsys):
    from wolf import engine

    def interrupted(self):  # Ctrl-C before the engine could emit anything itself
        raise KeyboardInterrupt

    monkeypatch.setattr(engine.Game, "run", interrupted)
    assert cli.main(["play", "--backend", "mock", "--seed", "2", "--log-dir", str(tmp_path)]) == 130
    out = capsys.readouterr().out
    assert "Log:" in out and "stopped by the user" in out
    (log,) = tmp_path.glob("*.jsonl")
    meta, events = load_game(log)
    assert events[-1].kind == "aborted" and "stats" in meta
    assert "stopped by the user" in log.with_suffix(".md").read_text(encoding="utf-8")


@needs_engine
def test_end_to_end_mock_game_and_replay(tmp_path):
    games = tmp_path / "games"
    play = subprocess.run([sys.executable, "-m", "wolf", "play", "--backend", "mock", "--seed", "1",
                           "--log-dir", str(games)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert play.returncode == 0, play.stdout[-3000:] + play.stderr[-3000:]
    logs = list(games.glob("*.jsonl"))
    assert len(logs) == 1 and logs[0].with_suffix(".md").exists()
    meta, events = load_game(logs[0])
    assert events[0].kind == "setup" and events[-1].kind == "game_over" and "stats" in meta
    replay = subprocess.run([sys.executable, "-m", "wolf", "replay", str(logs[0]), "--delay", "0"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert replay.returncode == 0, replay.stderr[-3000:]
    assert "Game over" in replay.stdout
    md = tmp_path / "again.md"
    write_markdown(meta, events, md)
    assert md.read_text(encoding="utf-8").startswith("# 🐺 Wolf")


def test_wrapped_lines_with_emoji_are_not_clipped():
    # rich ellipsizes a wrapped line that holds a wide emoji (🌾, 💭) even though it fits; the
    # display's "fold" overflow must keep every word of the speech intact.
    speech = ("Alice, let's go for Clara tonight—gotta start somewhere. Tomorrow I'll play agreeable, "
              "follow whoever sounds most convincing, and keep my head down until the vote.")
    events = [
        ev("setup", 0, "setup", visible_to=SPECTATOR, players=PLAYERS, config=CONFIG),
        ev("phase", 1, "night", "Night 1 falls over the village."),
        thought(1, "night", "Bram", "No data yet, so I'll pick an arbitrary starting point—random, really.",
                "wolf_chat"),
        ev("wolf_chat", 1, "night", speech, actor="Bram", target="Clara", visible_to=WOLVES),
    ]
    # The bug only bites when a wrapped line exactly fills the column, so sweep the widths.
    for width in range(60, 131):
        c = Console(width=width, record=True, file=io.StringIO())
        d = Display(console=c)
        for e in events:
            d.on_event(e)
        out = " ".join(c.export_text().split())
        assert "…" not in out, f"clipped at width {width}"
        for word in speech.split():
            assert word in out, f"{word!r} lost at width {width}"
