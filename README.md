# Wolf

Wolf is a game of Werewolf played entirely by AI agents in your terminal. Each player is a Claude
agent with a secret role (Werewolf, Doctor, Sheriff or Villager) and its own personality. They
scheme at night, argue and bluff by day, and vote each other out, while you watch the public chat,
each agent's private reasoning and the night actions. The rules are in **[RULES.md](RULES.md)**.

![The opening of a recorded game in god view: the wolves Pavel and Jonas agree to kill Silas, Silas the Sheriff investigates Pavel and finds a wolf, Silas is found dead at dawn, and on Day 1 Pavel opens the discussion while the villager Viktor says Pavel is steering it](docs/demo.gif)

*The opening of a recorded 9-player game on Sonnet, replayed in god view. On Night 1 the wolves
agree to kill Silas. Silas, the Sheriff, investigates Pavel and finds a wolf, but is dead by
dawn. On Day 1 Pavel opens the discussion, and Viktor, a villager, says Pavel is steering it. The
village voted Viktor out 7 to 1, both wolves voting with the crowd, and the wolves won on Night 3.*

## Setup

You need Python 3.12 or newer and [Claude Code](https://claude.com/claude-code), installed and
logged in. Run `claude` once and use `/login`. No API key is needed: the agents run on your Claude
subscription.

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Playing

```sh
.venv/bin/python -m wolf play                      # 9 players on Sonnet (the default)
.venv/bin/python -m wolf play --model haiku        # faster, and lighter on your usage limits
.venv/bin/python -m wolf play --backend mock       # free and instant, no AI: for testing the engine
```

Useful extras:

```sh
.venv/bin/python -m wolf play --view public        # hide roles and thoughts, as a villager would see it
.venv/bin/python -m wolf play --pause              # press Enter between phases
.venv/bin/python -m wolf play --show-notes         # also show each agent's private notes
.venv/bin/python -m wolf play --seed 42            # repeatable deal (a mock game is fully repeatable)
```

Watch a saved game again, with no AI calls:

```sh
.venv/bin/python -m wolf replay games/<file>.jsonl
```

## Flags

**`play`**

| Flag | Default | What it does |
|---|---|---|
| `--players N` | 9 | Number of players (at most 20) |
| `--wolves N` | 2 | Number of werewolves |
| `--no-doctor` | | Play without a Doctor |
| `--no-sheriff` | | Play without a Sheriff |
| `--max-rounds N` | 4 | Discussion rounds per day, at most (the talk also ends on a silent round, or when most players are ready to vote) |
| `--max-speeches N` | 4 | Times each player may speak per day, replies to direct questions included |
| `--max-days N` | 10 | The game is a draw if nobody has won after this day |
| `--seed N` | random | Seed for roles, names, personalities and speaking order |
| `--backend claude\|mock` | claude | `claude` runs real agents; `mock` makes random moves instantly, with no AI |
| `--model NAME` | sonnet | Claude model, e.g. `sonnet`, `haiku` or `opus` |
| `--effort LEVEL` | CLI default | Claude effort level: `low`, `medium`, `high`, `xhigh` or `max` |
| `--concurrency N` | 4 | Maximum number of Claude calls running at once (votes and night actions) |
| `--timeout SECONDS` | 180 | Time limit for a single Claude call |
| `--view god\|public` | god | `god` shows roles, thoughts and night actions; `public` shows only what a villager sees until the game ends |
| `--pause` | | Wait for Enter between phases |
| `--delay SECONDS` | 0 | Pause after each speech, vote and announcement |
| `--show-notes` | | Show each agent's private notes along with its thoughts |
| `--log-dir DIR` | games | Where game logs are written |
| `--no-log` | | Don't write any logs |

**`replay FILE`**

| Flag | Default | What it does |
|---|---|---|
| `--view god\|public` | god | As for `play` |
| `--pause` | | Wait for Enter between phases |
| `--delay SECONDS` | 0.4 | Pause after each speech, vote and announcement |
| `--show-notes` | | Show each agent's private notes along with its thoughts |
| `--md` | | Regenerate the game's Markdown transcript and exit, instead of replaying it |

## Game logs

Every game is saved in `games/` (or `--log-dir`) as `<YYYYmmdd-HHMMSS>.jsonl`, with one event per
line. That file is what `replay` reads. When the game ends, a readable Markdown transcript
(`.md`) is written beside it. Pass `--no-log` to skip both.

## Cost and time

Every decision an agent makes is one model call, and every call counts against your Claude plan's
usage limits, just like using Claude Code yourself.

Most calls are discussion turns. Every turn costs one call to decide whether to speak, and a turn
that wins the floor costs a second call to write the speech. A day's discussion costs at least one
call per living player (when everyone is quiet, or most are ready to vote after round 1); the turn
cap of five turns per living player bounds it at the other end, so a calm day costs much less than
a heated one. On top
of that, each day usually has one defense before the vote (two when the leans are tied), one vote
per living player, and each night one call each for the wolves, the Doctor and the Sheriff.

The timings below were measured before the current discussion system, with two fixed rounds
(two calls per living player per day); expect games to vary more now, and heated days to cost more.

- **Sonnet** (the default) plays best. In testing, a full 9-player game (3 days, 66 calls) took about
  14 minutes and reported $2.16 at API prices.
- **Haiku** (`--model haiku`) uses less of your limits per call, but it wasn't faster in testing: it
  reasoned privately at length, about 40 seconds per turn, so two days of a 7-player game took
  13 minutes (27 calls, $0.59 at API prices).
- **Mock** (`--backend mock`) is free and instant, but its players move at random.

Speeches happen one at a time; votes and night actions run in parallel.

At the end of a game, the summary shows the number of calls, the tokens used and a dollar figure.
That figure is the API list price the Claude CLI reports for the same usage. It is not a charge on
your subscription.

If you hit your usage limit mid-game, or your login expires, the game stops at the first such error
and tells you why. Other failures, such as timeouts, are retried; after 3 turns in a row fail, the game
stops. The log up to that point is kept.

## How it works

- **Every turn is stateless.** Each decision is a single headless `claude -p` call. The system
  prompt holds the rules, the player's role and personality. The turn prompt holds that player's
  game log and their private notes, which are the agent's only memory between turns. Agents run
  with all tools disabled, in an empty temporary directory, with no saved session.
- **The engine enforces hidden information.** Every game event records who may see it, and each
  agent's prompt is built only from the events that agent is allowed to see. Villagers never see
  the wolves' chat, and no agent ever sees another agent's thoughts.
- **Replies are structured JSON.** Each call passes a JSON schema, and the agent answers with fields
  such as `thought`, `speech`, `target` and `notes`. A discussion turn has two steps. First the
  agent thinks and returns an `urge` from 0 to 10, whether it is `ready_to_vote`, and its public
  `lean` (whom it would vote for right now), with no speech.
  Then the engine rolls the dice, and only a player who gets the floor is asked for the speech (and
  whom it `asks`), with its own thought handed back so the speech follows its plan. In god view
  you see every thought, score and roll; in public view, only what was said. Targets are limited to an `enum` of the legal
  names, so an agent can't vote for a dead player or protect someone twice in a row. An invalid
  reply is retried twice and then replaced with a random legal move, marked `[fallback]`. The game
  stops if three decisions in a row fail because Claude itself is unavailable.
- **Each role gets a playbook.** The system prompt coaches each role in concrete plays: the
  Sheriff leans toward revealing early and asks the Doctor for protection, the Doctor thinks about
  whom the wolves want dead and whom they expect it to protect, the wolves think about how a kill
  or a vote will look once roles are revealed, and villagers ask who gained from each death. Night
  actions and votes also ask the agent to look at the game from the other side first.
- **Calls run in parallel where the rules allow.** Votes and night actions run in parallel (up to
  `--concurrency`). Discussion runs one speaker at a time, because each speech must hear the ones
  before it.

The module contract is in [DESIGN.md](DESIGN.md).

## Tests

```sh
.venv/bin/python -m pytest -q
```

The tests never call Claude. The engine is tested on the mock backend, and the Claude backend
against a fake `claude` executable.
