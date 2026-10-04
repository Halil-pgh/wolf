"""The game loop: dealing, night -> day -> vote -> runoff, win checks and event emission.

See DESIGN.md for the event catalog and the threading rule: only the main thread reads or mutates game
state or emits events. Agent.execute calls may run in worker threads; their results are applied here in a
deterministic order.
"""

from __future__ import annotations

import random
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
from typing import Callable, Iterable

from . import prompts
from .agent import Agent
from .backends.base import BackendError, LLMBackend
from .config import GameConfig
from .personalities import deal_personas
from .roles import Role, Team
from .state import PLAYER_COLORS, PUBLIC, SPECTATOR, Decision, Discussion, Event, Player, Request, Task

MAX_BACKEND_FAILURES = 3  # consecutive decisions that fell back because of backend errors

_TASK_LABELS = {
    "discuss": "discussion turn",
    "speak": "speech",
    "wolf_chat": "wolf den message",
    "wolf_pick": "wolf pick",
    "protect": "protection",
    "investigate": "investigation",
    "vote": "vote",
    "runoff_vote": "runoff vote",
    "defense": "defense",
    "last_words": "last words",
}

# Why the day's discussion ended -> the public announcement.
_TALK_END = {
    "quiet": "A quiet moment falls over the village. Time to vote.",
    "ready": "Most of the village is ready to vote.",
    "cap": "The sun is setting. Time to vote.",
}

Call = tuple[Agent, Task, Request]


class GameAborted(Exception):
    """The game can't go on, usually because the backend keeps failing."""


class Game:
    def __init__(self, config: GameConfig, backend: LLMBackend, observers: Iterable = ()):
        config.validate()
        self.config = config
        self.backend = backend
        self.observers = list(observers)
        self.rng = random.Random(config.seed)

        roles = (
            [Role.WEREWOLF] * config.wolves
            + [Role.DOCTOR] * config.doctors
            + [Role.SHERIFF] * config.sheriffs
            + [Role.VILLAGER] * config.villagers
        )
        self.rng.shuffle(roles)
        personas = deal_personas(config.players, self.rng)
        self.players: list[Player] = [
            Player(name=persona.name, role=role, persona=persona, color=PLAYER_COLORS[i % len(PLAYER_COLORS)])
            for i, (persona, role) in enumerate(zip(personas, roles))
        ]
        self.by_name: dict[str, Player] = {p.name: p for p in self.players}
        self._seat = {p.name: i for i, p in enumerate(self.players)}
        self.agents: dict[str, Agent] = {p.name: Agent(p, self) for p in self.players}

        self.events: list[Event] = []
        self.requests: list[tuple[str, str, Request]] = []
        self.winner: Team | None = None
        self.day = 0
        self.phase = "setup"
        self.discussion = Discussion()  # today's; the discussion prompts read it
        self._turns_left = 0
        self._failures = 0
        self._pool: ThreadPoolExecutor | None = None
        self._started = False

    # ----------------------------------------------------------------------------------------
    # Plumbing
    # ----------------------------------------------------------------------------------------

    def emit(self, kind: str, text: str = "", *, actor: str | None = None, target: str | None = None,
             visible_to: tuple[str, ...] | None = PUBLIC, **data) -> Event:
        event = Event(kind=kind, day=self.day, phase=self.phase, text=text, actor=actor, target=target,
                      visible_to=visible_to, data=data)
        self.events.append(event)
        for observer in self.observers:
            observer.on_event(event)
        return event

    def busy(self, text: str) -> ExitStack:
        """Enter observer.busy(text) for every observer that has one."""
        stack = ExitStack()
        for observer in self.observers:
            busy = getattr(observer, "busy", None)
            if busy is not None:
                stack.enter_context(busy(text))
        return stack

    def living(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def _launch(self, calls: list[Call]) -> list[Callable[[], Decision]]:
        """Start agent calls. Returns one zero-argument callable per call that yields its Decision."""
        if self._pool is None:
            return [partial(agent.execute, task, req) for agent, task, req in calls]
        return [self._pool.submit(agent.execute, task, req).result for agent, task, req in calls]

    def _gather(self, handles: list[Callable[[], Decision]], text: str) -> list[Decision]:
        if not handles:
            return []
        with self.busy(text):
            return [handle() for handle in handles]

    def _ask_all(self, jobs: list[tuple[Player, Task]], text: str) -> list[Decision]:
        """Build every request (main thread), then run them together. The caller records the results."""
        calls = [(self.agents[p.name], task, self.agents[p.name].build(task)) for p, task in jobs]
        return self._gather(self._launch(calls), text)

    def _ask(self, player: Player, task: Task, text: str) -> Decision:
        decision = self._ask_all([(player, task)], text)[0]
        self._record(player, task, decision)
        return decision

    def _record(self, player: Player, task: Task, decision: Decision) -> None:
        if decision.notes:
            player.notes = decision.notes
        if decision.thought:
            self.emit("thought", decision.thought, actor=player.name, visible_to=SPECTATOR,
                      task=task.kind, notes=player.notes)
        if decision.fallback:
            self.emit("fallback", self._fallback_text(player, task, decision), actor=player.name,
                      visible_to=SPECTATOR)
        if decision.fallback and decision.backend_error:
            self._failures += 1
            if self._failures >= MAX_BACKEND_FAILURES:
                raise GameAborted(
                    f"The backend failed {self._failures} times in a row, so the game was stopped. "
                    f"Last error: {_shorten(decision.error)}. "
                    "Check that `claude` is logged in and not rate-limited, then try again."
                )
        elif not decision.auto:  # an automatic choice says nothing about the backend's health
            self._failures = 0

    @staticmethod
    def _fallback_text(player: Player, task: Task, decision: Decision) -> str:
        label = _TASK_LABELS.get(task.kind, task.kind)
        text = f"No valid reply from {player.name} for their {label} ({_shorten(decision.error) or 'unknown error'})."
        if decision.target:
            text += f" Picked {decision.target} at random."
        if task.speech_words is not None or task.urge:
            text += " They say nothing."
        return text

    def _kill(self, name: str, fate: str) -> None:
        player = self.by_name[name]
        player.alive = False
        player.fate = fate
        self.emit("death", f"{name} was {self._role_phrase(player.role)}.", target=name,
                  role=player.role.value, fate=fate)

    def _role_phrase(self, role: Role) -> str:
        cfg = self.config
        count = {Role.WEREWOLF: cfg.wolves, Role.DOCTOR: cfg.doctors, Role.SHERIFF: cfg.sheriffs,
                 Role.VILLAGER: cfg.villagers}[role]
        return f"{'the' if count == 1 else 'a'} {role.value}"

    # ----------------------------------------------------------------------------------------
    # The game
    # ----------------------------------------------------------------------------------------

    def run(self) -> Team | None:
        if self._started:
            raise RuntimeError("a Game can only be run once")
        self._started = True
        workers = int(getattr(self.backend, "max_concurrency", 1) or 1)
        if workers > 1:
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wolf-agent")
        try:
            self.phase = "setup"
            self.emit(
                "setup",
                visible_to=SPECTATOR,
                players=[
                    {"name": p.name, "role": p.role.value, "job": p.persona.job, "archetype": p.persona.archetype,
                     "quirk": p.persona.quirk, "color": p.color}
                    for p in self.players
                ],
                config=self.config.to_dict(),
            )
            for day in range(1, self.config.max_days + 1):
                self.day = day
                self._night()
                if self._check_win():
                    return self.winner
                self._day()
                if self._check_win():
                    return self.winner
            self._game_over(
                None,
                f"Day {self.config.max_days} ends with the werewolves still at large, so the game is a draw.",
            )
            return None
        except GameAborted as exc:
            self.phase = "end"
            self.emit("aborted", str(exc))
            raise
        except BackendError as exc:  # only fatal errors escape Agent.execute
            self.phase = "end"
            message = f"The game was stopped: {exc}"
            self.emit("aborted", message)
            raise GameAborted(message) from exc
        except KeyboardInterrupt:
            self.phase = "end"
            self.emit("aborted", "The game was stopped by the user.")
            raise
        finally:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None

    def _check_win(self) -> bool:
        wolves = sum(1 for p in self.players if p.alive and p.is_wolf)
        others = sum(1 for p in self.players if p.alive and not p.is_wolf)
        if wolves == 0:
            self._game_over(Team.VILLAGE, "All the werewolves are dead. The village wins!")
        elif wolves >= others:
            self._game_over(
                Team.WOLVES,
                "The werewolves are now as many as everyone else alive, and the village can no longer "
                "outvote them. The wolves win!",
            )
        else:
            return False
        return True

    def _game_over(self, winner: Team | None, text: str) -> None:
        self.winner = winner
        self.phase = "end"
        self.emit(
            "game_over",
            text,
            winner=winner.value if winner else None,
            roles={p.name: p.role.value for p in self.players},
            fates={p.name: p.fate for p in self.players},
        )

    # ----------------------------------------------------------------------------------------
    # Night
    # ----------------------------------------------------------------------------------------

    def _protect_targets(self, doctor: Player) -> list[str]:
        n = self.day
        last = doctor.protected[-1][1] if doctor.protected and doctor.protected[-1][0] == n - 1 else None
        return [
            p.name for p in self.living()
            if p.name != last and not (p is doctor and doctor.self_protect_used)
        ]

    def _investigate_targets(self, sheriff: Player) -> list[str]:
        return [p.name for p in self.living() if p is not sheriff and p.name not in sheriff.investigations]

    def _night(self) -> None:
        n = self.day
        self.phase = "night"
        self.emit("phase", f"Night {n} falls over the village.")

        # Doctor and Sheriff requests are built now and run in the background while the wolves act.
        jobs: list[tuple[Player, Task]] = []
        for p in self.living():
            if p.role is Role.DOCTOR and (targets := self._protect_targets(p)):
                jobs.append((p, prompts.protect_task(self, p, targets)))
        for p in self.living():
            if p.role is Role.SHERIFF and (targets := self._investigate_targets(p)):
                jobs.append((p, prompts.investigate_task(self, p, targets)))
        calls = [(self.agents[p.name], task, self.agents[p.name].build(task)) for p, task in jobs]
        handles = self._launch(calls)

        victim = self._wolves()

        decisions = self._gather(handles, self._night_busy_text(jobs))

        protected: set[str] = set()
        for (p, task), dec in zip(jobs, decisions):
            self._record(p, task, dec)
            if task.kind == "protect":
                p.protected.append((n, dec.target))
                if dec.target == p.name:
                    p.self_protect_used = True
                protected.add(dec.target)
                self.emit("protect", actor=p.name, target=dec.target, visible_to=(p.name,))
            else:
                is_wolf = self.by_name[dec.target].is_wolf
                p.investigations[dec.target] = is_wolf
                self.emit("investigate", actor=p.name, target=dec.target, visible_to=(p.name,), wolf=is_wolf)

        self.phase = "day"
        self.emit("phase", f"Day {n} dawns.")
        if victim in protected:
            self.emit("saved", target=victim, visible_to=SPECTATOR)
            self.emit("announce", "The village wakes, and everyone is still here. Nobody died last night.")
        else:
            self.emit("announce", f"The village wakes to terrible news: {victim} was killed in the night.",
                      target=victim)
            self._kill(victim, f"killed by the werewolves on Night {n}")

    @staticmethod
    def _night_busy_text(jobs: list[tuple[Player, Task]]) -> str:
        # Names no player: the spinner is also shown in the public view.
        roles = [f"the {r.value}" for r in (Role.DOCTOR, Role.SHERIFF) if any(p.role is r for p, _ in jobs)]
        if not roles:
            return "🌙 The night goes on…"
        who = prompts.join_names(roles)
        return f"🌙 {who[0].upper()}{who[1:]} {'is' if len(roles) == 1 else 'are'} at work…"

    def _wolves(self) -> str:
        pack = [p for p in self.players if p.alive and p.is_wolf]
        den = tuple(p.name for p in pack)
        targets = [p.name for p in self.players if p.alive and not p.is_wolf]
        busy = "🐺 The wolves are plotting…"

        if len(pack) >= 2:
            order = pack[:]
            self.rng.shuffle(order)
            proposals = []
            for i, wolf in enumerate(order):
                task = prompts.wolf_chat_task(self, wolf, targets, first=i == 0)
                dec = self._ask(wolf, task, busy)
                self.emit("wolf_chat", dec.speech, actor=wolf.name, target=dec.target, visible_to=den)
                proposals.append(dec.target)
            if len(set(proposals)) == 1:
                picks = proposals
            else:
                jobs = [(wolf, prompts.wolf_pick_task(self, wolf, targets, pack=True)) for wolf in pack]
                picks = []
                for (wolf, task), dec in zip(jobs, self._ask_all(jobs, busy)):
                    self._record(wolf, task, dec)
                    self.emit("wolf_pick", actor=wolf.name, target=dec.target, visible_to=den)
                    picks.append(dec.target)
        else:
            wolf = pack[0]
            task = prompts.wolf_pick_task(self, wolf, targets, pack=False)
            dec = self._ask(wolf, task, busy)
            self.emit("wolf_pick", actor=wolf.name, target=dec.target, visible_to=den)
            picks = [dec.target]

        tally = Counter(picks)
        top = max(tally.values())
        tied = [name for name in targets if tally[name] == top]  # seat order keeps this deterministic
        victim = tied[0] if len(tied) == 1 else self.rng.choice(tied)
        self.emit("wolf_decision", f"The pack will attack {victim}.", target=victim, visible_to=den,
                  random=len(tied) > 1)
        return victim

    # ----------------------------------------------------------------------------------------
    # Day
    # ----------------------------------------------------------------------------------------

    def _day(self) -> None:
        d = self.day
        self._discussion()
        self._trial()

        voters = self.living()
        names = [p.name for p in voters]
        tally = self._votes(
            [(p, prompts.vote_task(self, p, [n for n in names if n != p.name])) for p in voters],
            runoff=False, text="🗳️ The village is voting…",
        )
        leaders, counts = self._leaders(tally)
        out: str | None = None
        if len(leaders) == 1:
            out = leaders[0]
            self.emit("vote_result", f"{counts} · {out} is eliminated.", target=out,
                      tally=self._ordered(tally), tied=[], runoff=False)
        else:
            self.emit(
                "vote_result",
                f"{counts} · Tied between {prompts.join_names(leaders)}: they will defend themselves, "
                "then everyone votes again.",
                tally=self._ordered(tally), tied=leaders, runoff=False,
            )
            order = [self.by_name[n] for n in leaders]
            self.rng.shuffle(order)
            for p in order:
                dec = self._ask(p, prompts.defense_task(self, p, leaders), f"💬 {p.name} is preparing a defense…")
                self.emit("defense", dec.speech, actor=p.name)

            voters = self.living()
            tally = self._votes(
                [(p, prompts.runoff_task(self, p, [n for n in leaders if n != p.name], leaders)) for p in voters],
                runoff=True, text="🗳️ The village votes again…",
            )
            leaders2, counts = self._leaders(tally)
            if len(leaders2) == 1:
                out = leaders2[0]
                self.emit("vote_result", f"{counts} · {out} is eliminated.", target=out,
                          tally=self._ordered(tally), tied=[], runoff=True)
            else:
                self.emit("vote_result", f"{counts} · Still tied, so nobody is eliminated today.",
                          tally=self._ordered(tally), tied=leaders2, runoff=True)

        if out is not None:
            p = self.by_name[out]
            dec = self._ask(p, prompts.last_words_task(self, p), f"💬 {p.name} is choosing last words…")
            self.emit("last_words", dec.speech, actor=p.name)
            self._kill(out, f"voted out on Day {d}")

    def _discussion(self) -> None:
        """Rounds around the table until a quiet round, a ready-to-vote majority, or a cap."""
        cfg = self.config
        living = self.living()
        talk = self.discussion = Discussion()
        self._turns_left = cfg.day_turns_per_player * len(living)
        reason = "cap"
        for rnd in range(1, cfg.max_rounds + 1):
            if all(talk.spoken.get(p.name, 0) >= cfg.max_speeches for p in living):
                break  # everyone has used up their speeches
            talk.round = rnd
            heard, end = self._round(living)
            if end is None and rnd == 1 and self._most_ready(living):
                end = "ready"  # round 1 always finishes before readiness can end the talk
            if end is None and not heard:
                end = "quiet"
            if end is not None:
                reason = end
                break
        self.emit("discussion_end", _TALK_END[reason], reason=reason, round=talk.round)

    def _round(self, living: list[Player]) -> tuple[bool, str | None]:
        """One round: everyone in a fresh random order, with replies jumping the queue.
        Returns (whether anyone spoke, "ready" or "cap" if the discussion ends mid-round)."""
        cfg, talk = self.config, self.discussion
        order = living[:]
        self.rng.shuffle(order)
        pending = deque(order)
        replies: deque[tuple[Player, str]] = deque()  # (player, who asked them), answered next
        had_turn: set[str] = set()
        chain = 0  # out-of-turn replies in a row
        heard = False
        while pending or replies:
            if replies and chain < cfg.max_replies_in_row:
                p, asker = replies.popleft()
            else:
                replies.clear()  # the chain is long enough: the round carries on
                if not pending:
                    break
                p, asker = pending.popleft(), None
                if p.name in had_turn:  # a reply already used up this turn
                    continue
            if talk.spoken.get(p.name, 0) >= cfg.max_speeches:
                continue  # done talking for today: the turn is skipped, with no call
            if self._turns_left <= 0:
                return heard, "cap"
            self._turns_left -= 1
            chain = chain + 1 if asker else 0
            had_turn.add(p.name)
            spoke, asks = self._turn(p, asker)
            if spoke:
                heard = True
                queued = {q.name for q, _ in replies}
                replies.extend((self.by_name[n], p.name) for n in asks if n not in queued)
            if talk.round > 1 and self._most_ready(living):
                return heard, "ready"
        return heard, None

    def _turn(self, p: Player, reply_to: str | None) -> tuple[bool, list[str]]:
        """One discussion turn. First call: think and rate the urge (no speech yet). Then the dice: the
        player gets the floor with probability urge/10. Only then a second call writes the speech, knowing
        it will be heard. Returns (whether they spoke, whom their speech asked)."""
        talk = self.discussion
        dec = self._ask(p, prompts.discuss_task(self, p, reply_to), f"💬 {p.name} is thinking…")
        askers = talk.asked_by.pop(p.name, [])  # they get their chance to answer now
        urge = dec.urge or 0
        roll = self.rng.randint(1, 10)
        floor = roll <= urge
        if urge and not floor:
            talk.hesitated.add(p.name)
        else:
            talk.hesitated.discard(p.name)
        was_ready = talk.ready.get(p.name, False)
        if dec.ready_to_vote is not None:
            talk.ready[p.name] = dec.ready_to_vote
        ready = talk.ready.get(p.name, False)
        was_lean = talk.leans.get(p.name, "")
        if dec.lean is not None:
            talk.leans[p.name] = dec.lean
        lean = talk.leans.get(p.name, "")
        self.emit("turn", actor=p.name, visible_to=SPECTATOR, urge=urge, roll=roll, spoke=floor,
                  ready_to_vote=ready, round=talk.round, reply_to=reply_to)

        asks: list[str] = []
        spoke = False
        if floor:
            task = prompts.speak_task(self, p, urge, dec.thought, askers, reply_to)
            said = self._ask(p, task, f"🗣️ {p.name} has the floor…")
            spoke = bool(said.speech)  # empty only if the speech call failed
        if spoke:
            asks = list(said.asks)
            talk.spoken[p.name] = talk.spoken.get(p.name, 0) + 1
            for name in asks:
                others = talk.asked_by.setdefault(name, [])
                if p.name not in others:
                    others.append(p.name)
            self.emit("speech", said.speech, actor=p.name, round=talk.round, reply_to=reply_to, asks=asks)
        if lean != was_lean:
            self.emit("lean", actor=p.name, target=lean or None, round=talk.round,
                      tally=self._ordered(self._lean_tally()))
        if ready != was_ready:
            living = self.living()
            self.emit("ready", actor=p.name, ready=ready, round=talk.round, living=len(living),
                      count=sum(talk.ready.get(q.name, False) for q in living))
        return spoke, asks

    def _most_ready(self, living: list[Player]) -> bool:
        return 2 * sum(self.discussion.ready.get(p.name, False) for p in living) > len(living)

    def _lean_tally(self) -> Counter:
        """How many living players lean toward each player right now."""
        leans = self.discussion.leans
        return Counter(leans[p.name] for p in self.living() if leans.get(p.name))

    def _trial(self) -> None:
        """Before the vote, the player most players lean toward (at least 2) gets a defense, even with no
        speeches left; two players tied for most both defend, and 3 or more tied means nobody stands out.
        No replies: the vote follows at once."""
        tally = self._lean_tally()
        if not tally:
            return
        top = max(tally.values())
        accused = [p.name for p in self.living() if tally[p.name] == top]  # seat order
        if top < 2 or len(accused) > 2:
            return
        living = len(self.living())
        if len(accused) == 1:
            (name,) = accused
            text = (f"The village turns to {name}: {top} of {living} lean toward voting them out. "
                    f"Before the vote, {name} may speak in their defense.")
        else:
            text = (f"The village is split between {prompts.join_names(accused)}, with {top} leaning toward "
                    "each. Before the vote, both may speak in their defense.")
        self.emit("trial", text, target=accused[0] if len(accused) == 1 else None, accused=accused,
                  tally=self._ordered(tally))
        order = [self.by_name[n] for n in accused]
        self.rng.shuffle(order)
        for p in order:
            task = prompts.trial_task(self, p, accused)
            dec = self._ask(p, task, f"💬 {p.name} is preparing a defense…")
            self.emit("defense", dec.speech, actor=p.name, trial=True)

    def _votes(self, jobs: list[tuple[Player, Task]], runoff: bool, text: str) -> Counter:
        tally: Counter = Counter()
        for (p, task), dec in zip(jobs, self._ask_all(jobs, text)):
            self._record(p, task, dec)
            self.emit("vote", actor=p.name, target=dec.target, reason=dec.reason, runoff=runoff, auto=dec.auto)
            tally[dec.target] += 1
        return tally

    def _ordered(self, tally: Counter) -> dict[str, int]:
        return {n: tally[n] for n in sorted(tally, key=lambda n: (-tally[n], self._seat[n]))}

    def _leaders(self, tally: Counter) -> tuple[list[str], str]:
        ordered = self._ordered(tally)
        top = max(ordered.values())
        leaders = [n for n, c in ordered.items() if c == top]
        counts = ", ".join(f"{n} {c}" for n, c in ordered.items())
        return leaders, counts


def _shorten(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
