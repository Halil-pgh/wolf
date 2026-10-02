# Wolf: Design & Integration Contract

This is the contract between the modules. The game rules are in `RULES.md`, and the approved plan they come from is summarized there.
**Already written; don't change their public shapes:** `wolf/roles.py`, `wolf/config.py`, `wolf/personalities.py`, `wolf/state.py` and `wolf/backends/base.py`.

Python 3.12, run with `/home/halil/Documents/wolf/.venv/bin/python`. The only dependencies are `rich` (runtime) and `pytest` (tests).

## Module ownership
| Module | Owner | Purpose |
|---|---|---|
| `wolf/engine.py` | core | `Game`: dealing, the night → day → vote → runoff loop, win checks, event emission |
| `wolf/agent.py` | core | `Agent`: builds requests, calls the backend, validates, retries, falls back |
| `wolf/prompts.py` | core | system prompts, turn prompts, JSON schemas, per-viewer game log |
| `wolf/backends/mock.py` | core | `MockBackend`: instant, seeded, no AI |
| `tests/test_engine.py` | core | engine and hidden-information tests on the mock backend |
| `wolf/display.py` | presentation | `Display`: rich terminal output, god or public view |
| `wolf/logger.py` | presentation | `GameLogger` (JSONL and Markdown), `load_game()` for replay |
| `wolf/cli.py`, `wolf/__main__.py` | presentation | the `play` and `replay` commands |
| `tests/test_display_logger.py` | presentation | display and logger tests on synthetic events |
| `wolf/backends/claude_cli.py` | backend | `ClaudeCLIBackend`: headless `claude -p` |
| `tests/test_claude_cli.py` | backend | tests using a fake `claude` executable, plus an opt-in live test |
| `RULES.md`, `README.md` | backend | player-facing rules; setup and usage |

## Pinned signatures
```python
# wolf/engine.py
class GameAborted(Exception): ...
class Game:
    def __init__(self, config: GameConfig, backend: LLMBackend, observers: Iterable = ()): ...
    def run(self) -> Team | None: ...   # emits everything, starting with the "setup" event; returns the winner or None for a draw
    players: list[Player]                # in seat order
    by_name: dict[str, Player]
    events: list[Event]
    winner: Team | None
    requests: list[tuple[str, str, Request]]   # (player name, task kind, request) for every built request: used by tests and debugging
    discussion: Discussion               # today's talk so far (state.py); the discussion prompts read it

# wolf/agent.py
class Agent:
    def __init__(self, player: Player, game: "Game"): ...
    def build(self, task: Task) -> Request: ...           # MAIN THREAD ONLY (reads game state)
    def execute(self, task: Task, req: Request) -> Decision: ...  # thread-safe; never mutates game state

# wolf/prompts.py
def system_prompt(game, player) -> str
def turn_prompt(game, player, task) -> str
def schema_for(task: Task) -> dict
def log_line(event: Event) -> str | None   # how an event reads in an agent's game log; None = not shown

# wolf/backends/mock.py
class MockBackend:
    def __init__(self, seed: int | None = None, error_rate: float = 0.0): ...   # max_concurrency = 1

# wolf/backends/claude_cli.py
class ClaudeCLIBackend:
    def __init__(self, model: str = "sonnet", effort: str | None = None, timeout: float = 180.0,
                 concurrency: int = 4, claude_bin: str = "claude"): ...

# wolf/display.py
class Display:
    def __init__(self, console=None, view: str = "god", pause: bool = False, delay: float = 0.0, show_notes: bool = False): ...
    def on_event(self, event: Event) -> None: ...
    def busy(self, text: str): ...   # context manager; shows a spinner while agents think
    def summary(self, stats: dict) -> None: ...  # prints backend stats after the game

# wolf/logger.py
class GameLogger:
    def __init__(self, directory: str | Path, meta: dict): ...   # creates <directory>/<YYYYmmdd-HHMMSS>.jsonl
    path: Path
    def on_event(self, event: Event) -> None: ...   # appends a line, flushes, and writes the .md transcript on game_over or aborted
    def close(self, stats: dict | None = None) -> None: ...
def load_game(path) -> tuple[dict, list[Event]]
def write_markdown(meta: dict, events: list[Event], path) -> None
```

## Observers
The engine calls `observer.on_event(event)` for every event, in order, **always on the main thread**. If an observer has a `busy(text)` method, the engine wraps every wait on the backend in `with observer.busy(text):`.

## Threading rule
Only the main thread reads or mutates game state or emits events. `Agent.execute` runs in worker threads and only calls the backend. The engine builds every request first, runs the `execute` calls, and then applies the results and emits events in a deterministic order. When `backend.max_concurrency == 1`, everything runs in sequence, which makes mock games fully reproducible from the seed.

## Backend stats keys
`stats()` returns a dict containing at least `calls`, `errors` and `seconds` (the total wall time spent in calls). The Claude backend also returns `input_tokens`, `output_tokens` and `cost_usd`, which is the API-equivalent list price reported by the CLI. Display prints whatever keys are present.

## Event catalog
The `visible_to` column uses these values: **public** is `None`, **spectator** is `()`, **pack** is the tuple of living wolf names, and **self** is `(actor,)`. The display renders events from their structured fields. It prints `text` verbatim only for these kinds: `phase`, `announce`, `vote_result`, `discussion_end`, `fallback`, `game_over` and `aborted`.
Logs from before the urge-to-speak discussion (2026-10-01) also hold `pass` events (a silent player, with `round`); the display and the Markdown transcript still render them, so old games replay.

| kind | visible_to | actor | target | text | data |
|---|---|---|---|---|---|
| `setup` | spectator | | | "" | `players`: list of {name, role, job, archetype, quirk, color}; `config`: GameConfig.to_dict() |
| `phase` | public | | | "Night 1 falls over the village." / "Day 1 dawns." | |
| `thought` | spectator | player | | the character's inner monologue (the `thought` field) | `task`: task kind; `notes`: the new notes |
| `fallback` | spectator | player | | why a random choice was made | |
| `wolf_chat` | pack | wolf | proposed victim | what the wolf said | |
| `wolf_pick` | pack | wolf | picked victim | "" | |
| `wolf_decision` | pack | | victim | "The pack will attack X." | `random`: bool (split picks) |
| `protect` | self | doctor | protected | "" | |
| `investigate` | self | sheriff | investigated | "" | `wolf`: bool |
| `saved` | spectator | | saved player | "" | |
| `announce` | public | | victim or None | the morning announcement | |
| `death` | public | | dead player | "X was a Werewolf." | `role`: role value; `fate`: e.g. "voted out on Day 2" |
| `turn` | spectator | player | | "" | `urge`: 0–10; `roll`: 1–10; `spoke`: bool, got the floor (`roll <= urge`), so a `speech` follows unless the speech call failed; `ready_to_vote`: bool; `round`: int; `reply_to`: asker or None |
| `speech` | public | player | | what they said | `round`: int; `reply_to`: asker or None (an out-of-turn reply); `asks`: [names] |
| `ready` | public | player | | "" | `ready`: bool (their new readiness); `count`: how many living players are ready; `living`: int; `round`: int |
| `discussion_end` | public | | | why the talk ended, e.g. "A quiet moment falls over the village. Time to vote." | `reason`: "quiet" / "ready" / "cap"; `round`: the last round |
| `vote` | public | voter | voted-for | "" | `reason`: str; `runoff`: bool; `auto`: bool |
| `vote_result` | public | | eliminated or None | e.g. "Bram 3, Alice 2 · Bram is eliminated." | `tally`: {name: count}; `tied`: [names]; `runoff`: bool |
| `defense` | public | tied player | | their defense | |
| `last_words` | public | eliminated | | their last words | |
| `game_over` | public | | | e.g. "All werewolves are dead. The village wins!" | `winner`: "village" / "wolves" / None; `roles`: {name: role}; `fates`: {name: fate or None} |
| `aborted` | public | | | why the game stopped | |

## Order of one day/night cycle
1. **`phase` (Night N).** Doctor and Sheriff requests are built and submitted first. Their calls run in the background while the wolves act, on the main thread:
   - With 2 or more wolves: `wolf_chat`, one per wolf in random order, each seeing the chat so far.
     - If the proposals disagree, every wolf casts a parallel `wolf_pick`.
     - If the picks still differ, a random pick wins.
   - With a single wolf: one `wolf_pick`.
   - Then `wolf_decision` is emitted.
2. **Doctor and Sheriff results:** `thought` + `protect`, and `thought` + `investigate`.
3. **Morning:** `phase` (Day N), then:
   - either `saved` + `announce` ("Nobody died");
   - or `announce` + `death`.
4. **Win check.**
5. **Discussion:** rounds around the table, each in a fresh random order. A turn is two steps:
   - A `discuss` call returns `thought`, `urge` (0–10), `ready_to_vote` and `notes`, and no speech. The engine emits `thought`, rolls 1–10 with `game.rng`, and emits `turn`; the player gets the floor if the roll ≤ urge.
   - Only if they got the floor, a `speak` call (whose task hands back their own thought from the first call) returns `speech` and `asks`, with no thought or notes. The engine emits `speech`, or a `fallback` if that call failed.
   - Then `ready` if their readiness changed, on silent turns too.
   - **Replies:** the players named in a heard speech's `asks` are asked next, first come first served. A reply uses up the asked player's turn this round, or is an extra turn if they already had it. After `max_replies_in_row` replies in a row, the queue is dropped and the round carries on.
   - **Skipped:** a player who has spoken `max_speeches` times today gets no more turns (and no calls).
   - **The end**, whichever comes first: a round with no speech (`quiet`); more than half the living players ready to vote, checked after every turn from round 2 and at the end of round 1 (`ready`); or `max_rounds` rounds, `day_turns_per_player` × living turns, or everyone out of speeches (`cap`). Then `discussion_end`.
6. **Vote:** every living player votes in parallel. Then the `thought`s and `vote`s are emitted in seat order, followed by `vote_result`.
   - **On a tie:** the tied players give `defense`s, then there is a parallel runoff, which is also emitted as `vote` events with `runoff: true`, and a second `vote_result`.
7. **Elimination:** `thought` + `last_words`, then `death`.
8. **Win check.** After the day `max_days`, the game is a draw.

`game_over` is always the last event, unless `aborted` replaces it. The engine raises `GameAborted` after 3 consecutive decisions that fell back because of backend errors. It also raises it at once on a `BackendError` with `fatal=True`, which the Claude backend uses for "not logged in" and "usage limit reached": `Agent.execute` re-raises those without retrying.
