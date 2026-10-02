"""Command line: `python -m wolf play …` and `python -m wolf replay FILE …`."""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text

from .config import GameConfig
from .display import Display
from .logger import GameLogger, load_game, write_markdown
from .state import Event


class SetupError(Exception):
    """A problem the user can fix; shown as a one-line error with exit code 2."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wolf", description="AI Werewolf: every player is an AI agent, and you watch.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{play,replay}")

    play = sub.add_parser("play", help="play a new game", description="Deal a new game and watch it.")
    game = play.add_argument_group("game")
    game.add_argument("--players", type=int, default=GameConfig.players, help="number of players (default: %(default)s)")
    game.add_argument("--wolves", type=int, default=GameConfig.wolves, help="number of werewolves (default: %(default)s)")
    game.add_argument("--no-doctor", action="store_true", help="play without a Doctor")
    game.add_argument("--no-sheriff", action="store_true", help="play without a Sheriff")
    game.add_argument("--max-rounds", type=int, default=GameConfig.max_rounds,
                      help="discussion rounds per day, at most (default: %(default)s)")
    game.add_argument("--max-speeches", type=int, default=GameConfig.max_speeches,
                      help="times each player may speak per day, replies included (default: %(default)s)")
    game.add_argument("--max-days", type=int, default=GameConfig.max_days, help="draw after this many days (default: %(default)s)")
    game.add_argument("--seed", type=int, default=None, help="random seed (default: a random number, printed)")

    ai = play.add_argument_group("AI backend")
    ai.add_argument("--backend", choices=["claude", "mock"], default="claude",
                    help="claude = headless `claude -p` on your plan; mock = instant, no AI (default: %(default)s)")
    ai.add_argument("--model", default="sonnet", help="Claude model, e.g. sonnet or haiku (default: %(default)s)")
    ai.add_argument("--effort", default=None, help="reasoning effort passed to the model (default: none)")
    ai.add_argument("--concurrency", type=int, default=4, help="parallel model calls (default: %(default)s)")
    ai.add_argument("--timeout", type=float, default=180.0, help="seconds per model call (default: %(default)s)")

    out = play.add_argument_group("output")
    _add_view_args(out, delay=0.0)
    out.add_argument("--log-dir", default="games", help="where to save the game log (default: %(default)s)")
    out.add_argument("--no-log", action="store_true", help="don't save a log or transcript")

    replay = sub.add_parser("replay", help="re-watch a saved game (no AI calls)",
                            description="Re-watch a saved game log. Makes no AI calls.")
    replay.add_argument("file", help="a games/<timestamp>.jsonl log")
    _add_view_args(replay, delay=0.4)
    replay.add_argument("--md", action="store_true", help="regenerate the Markdown transcript and exit")
    return parser


def _add_view_args(group, delay: float) -> None:
    group.add_argument("--view", choices=["god", "public"], default="god",
                       help="god = see every secret; public = only what the village sees (default: %(default)s)")
    group.add_argument("--pause", action="store_true", help="press Enter before each night and day")
    group.add_argument("--delay", type=float, default=delay,
                       help="seconds to wait after each speech, vote or announcement (default: %(default)s)")
    group.add_argument("--show-notes", action="store_true", help="also show each agent's private notes")


def _fail(message: str) -> int:
    Console(stderr=True).print(Text.assemble(("wolf: error: ", "bold red"), message), highlight=False)
    return 2


def _make_backend(args, seed: int):
    if args.backend == "mock":
        try:
            from .backends.mock import MockBackend
        except ImportError as exc:
            raise SetupError(f"the mock backend is not available ({exc})") from exc
        return MockBackend(seed=seed)
    if shutil.which("claude") is None:
        raise SetupError("the `claude` command was not found on your PATH. Install Claude Code and log in "
                         "(run `claude` once), or try `--backend mock`.")
    try:
        from .backends.claude_cli import ClaudeCLIBackend
    except ImportError as exc:
        raise SetupError(f"the Claude backend is not available ({exc})") from exc
    from .backends.base import BackendError

    try:
        return ClaudeCLIBackend(model=args.model, effort=args.effort, timeout=args.timeout,
                                concurrency=args.concurrency)
    except (FileNotFoundError, ValueError, BackendError) as exc:
        raise SetupError(f"can't start the Claude backend: {exc}") from exc


def cmd_play(args, console: Console | None = None) -> int:
    console = console or Console()
    seed = args.seed if args.seed is not None else random.randrange(1, 1_000_000)
    config = GameConfig(
        players=args.players, wolves=args.wolves, doctors=0 if args.no_doctor else 1,
        sheriffs=0 if args.no_sheriff else 1, max_rounds=args.max_rounds, max_speeches=args.max_speeches,
        max_days=args.max_days, seed=seed,
    )
    try:
        config.validate()
        if args.concurrency < 1:
            raise ValueError("--concurrency must be at least 1")
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        if args.delay < 0:
            raise ValueError("--delay can't be negative")
    except ValueError as exc:
        return _fail(f"invalid game setup: {exc}")
    try:
        backend = _make_backend(args, seed)
    except SetupError as exc:
        return _fail(str(exc))
    from .engine import Game, GameAborted

    display = Display(console=console, view=args.view, pause=args.pause, delay=args.delay,
                      show_notes=args.show_notes)
    logger = None
    if not args.no_log:
        meta = {
            "config": config.to_dict(),
            "backend": args.backend,
            "model": args.model if args.backend == "claude" else None,
            "effort": args.effort if args.backend == "claude" else None,
            "seed": seed,
            "view": args.view,
            "started": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            logger = GameLogger(args.log_dir, meta)
        except OSError as exc:
            return _fail(f"can't create a log in {args.log_dir}: {exc}")
    observers = [display] + ([logger] if logger else [])

    console.print(Text.assemble(("🎲 Seed ", "dim"), (str(seed), "bold"),
                                (f"  (deal the same game again with --seed {seed})", "dim")))
    code = 0
    game = None
    started = time.monotonic()
    stats: dict | None = None
    try:
        try:
            game = Game(config, backend, observers)
            game.run()
        except GameAborted as exc:
            code = 1
            _emit_end(observers, game, str(exc) or "The game was aborted.")
        except KeyboardInterrupt:
            code = 130
            _emit_end(observers, game, "The game was stopped by the user.")
        try:
            stats = dict(backend.stats() or {})
        except Exception:
            stats = {}
        stats["wall_seconds"] = round(time.monotonic() - started, 1)
        display.summary(stats)
    finally:
        if logger is not None:
            logger.close(stats)
            log, md = _shown(logger.path), _shown(logger.md_path)
            # soft_wrap: long paths stay unbroken, so they can be copied
            console.print(Text.assemble(("📝 Log: ", "bold"), log), soft_wrap=True, highlight=False)
            if logger.md_path.exists():
                console.print(Text.assemble(("📜 Transcript: ", "bold"), md), soft_wrap=True, highlight=False)
            console.print(Text(f"   Re-watch with: python -m wolf replay {log}", style="dim"), soft_wrap=True,
                          highlight=False)
    return code


def _shown(path: Path) -> str:
    """The path relative to the current directory when it is inside it."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _emit_end(observers, game, text: str) -> None:
    """Close the story with an `aborted` event, unless the engine already emitted game_over/aborted."""
    events = getattr(game, "events", None) or []
    if events and events[-1].kind in ("game_over", "aborted"):
        return
    day = events[-1].day if events else 0
    end = Event(kind="aborted", day=day, phase="end", text=text)
    for observer in observers:
        try:
            observer.on_event(end)
        except Exception:
            pass


def cmd_replay(args, console: Console | None = None) -> int:
    console = console or Console()
    path = Path(args.file)
    try:
        meta, events = load_game(path)
    except FileNotFoundError:
        return _fail(f"no such file: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"can't read {path}: {exc}")
    if not events:
        return _fail(f"{path} has no game events (is it a wolf .jsonl log?)")
    if args.md:
        md = path.with_suffix(".md")
        write_markdown(meta, events, md)
        console.print(Text.assemble(("📜 Transcript: ", "bold"), _shown(md)), soft_wrap=True, highlight=False)
        return 0
    if args.delay < 0:
        return _fail("--delay can't be negative")

    info = [path.name]
    if meta.get("backend"):
        info.append("/".join(str(x) for x in (meta["backend"], meta.get("model")) if x))
    if meta.get("seed") is not None:
        info.append(f"seed {meta['seed']}")
    if meta.get("started"):
        info.append(f"played {str(meta['started']).replace('T', ' ')}")
    console.print(Text("▶ Replaying " + " · ".join(info), style="dim"), highlight=False)

    display = Display(console=console, view=args.view, pause=args.pause, delay=args.delay,
                      show_notes=args.show_notes)
    try:
        for event in events:
            display.on_event(event)
    except KeyboardInterrupt:
        console.print()
        console.print(Text("Replay stopped.", style="dim"))
        return 130
    if meta.get("stats"):
        display.summary(meta["stats"])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "play":
        return cmd_play(args)
    return cmd_replay(args)


if __name__ == "__main__":
    sys.exit(main())
