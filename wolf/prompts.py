"""What each agent reads: system prompts, turn prompts, task texts, JSON schemas and the game log.

Hidden information is enforced here. An agent's game log is built only from the events it can see
(`Event.visible(name)`), and its "private knowledge" section only from its own role and memory.
Nothing about other players' thoughts, notes or hidden roles is ever included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from .personalities import URGE_HINTS
from .roles import Role
from .state import Event, Player, Task

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Game

NOTES_WORDS = 100  # the agent is asked to keep its notes under this many words

# Event kinds that never appear in an agent's game log.
_HIDDEN_KINDS = {"setup", "thought", "fallback", "saved", "turn", "game_over", "aborted"}


def join_names(names: Iterable[str]) -> str:
    """'Alice', 'Alice and Bram', 'Alice, Bram and Clara'."""
    names = list(names)
    if not names:
        return "nobody"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _the(count: int, role: str) -> str:
    return f"The {role}" if count == 1 else f"Each {role}"


# --------------------------------------------------------------------------------------------
# System prompt (constant per player)
# --------------------------------------------------------------------------------------------

def system_prompt(game: "Game", player: Player) -> str:
    persona = player.persona
    others = [p.name for p in game.players if p is not player]
    intro = (
        f"You are {player.name}, the village {persona.job}. You are playing Werewolf, "
        f"a social deduction game, with: {', '.join(others)}."
    )
    return "\n\n".join(
        [intro, _rules(game), _role_brief(game, player), _personality(player), _response_rules(player)]
    )


def _rules(game: "Game") -> str:
    cfg = game.config
    rounds = "1 round" if cfg.max_rounds == 1 else f"{cfg.max_rounds} rounds"
    times = "once" if cfg.max_speeches == 1 else f"{cfg.max_speeches} times"
    lines = [
        "# The rules",
        f"- There are {cfg.players} players: {cfg.role_summary()}. Every role is secret. "
        "The werewolves know each other; everyone else knows only their own role.",
        "- The game starts at night. Each night:",
        "  - The werewolves meet in secret and choose one player who is not a werewolf to kill.",
    ]
    if cfg.doctors:
        lines.append(
            f"  - {_the(cfg.doctors, 'Doctor')} protects one living player from the attack. A Doctor can't protect "
            "the same player two nights in a row, and can protect themself only once per game. "
            "If the werewolves attack a protected player, nobody dies."
        )
    if cfg.sheriffs:
        lines.append(
            f"  - {_the(cfg.sheriffs, 'Sheriff')} investigates one player and privately learns whether they are a werewolf."
        )
    lines += [
        "- Each morning the village learns who was killed, or that nobody died. Nobody is told who was attacked or protected.",
        "- Each day the village talks before it votes. The discussion goes in rounds: in each round every living "
        "player gets a turn, in a fresh random order, and either speaks up or keeps quiet. Anyone asked a direct "
        f"question gets to answer right away, out of turn. Nobody may speak more than {times} a day.",
        "- On every turn, each player also says whom they would vote for if the vote were now (their \"lean\"), or "
        "nobody. Leans are public: everyone sees each player's latest lean, even when that player stays quiet.",
        "- The discussion ends when a whole round goes by in silence, when more than half of the living players "
        f"are ready to vote (but not before everyone has had a first turn), or after at most {rounds}.",
        "- When the discussion ends, the player most people lean toward (at least 2 of them) gets one last defense "
        "before the vote, even if they have used up their speeches; if two players are tied for most, both defend. "
        "Then the vote follows at once, with no more talk.",
        "- Then everyone votes at the same time to eliminate one living player. Nobody may abstain, and every "
        "ballot is revealed along with its reason. The player with the most votes says their last words and is eliminated.",
        "- If the vote is tied, each tied player gives a short defense, then everyone votes again among the tied "
        "players only (a tied player can't vote for themself). If the runoff is also tied, nobody is eliminated that day.",
        "- When a player dies, their role is revealed to everyone. The dead take no further part in the game.",
        "- The village wins when every werewolf is dead. The werewolves win as soon as they are at least as many "
        f"as everyone else alive. If neither side has won after Day {cfg.max_days}, the game is a draw.",
        "- Anyone may claim any role, and anyone may lie.",
    ]
    return "\n".join(lines)


def _role_brief(game: "Game", player: Player) -> str:
    cfg = game.config
    village_goal = "you win when every werewolf is dead, even if you die along the way."

    if player.role is Role.WEREWOLF:
        mates = [p.name for p in game.players if p.is_wolf and p is not player]
        if mates:
            pack = (
                f"The pack is you and {join_names(mates)}; everyone else is on the village team. "
                "Each night the pack talks in the wolf den, where only werewolves can hear, and chooses a victim."
            )
        else:
            pack = "You are the only werewolf; everyone else is on the village team. Each night you choose a victim alone."
        tactics = [
            "- Blend in. By day, talk like a worried villager: share suspicions, ask questions and vote like someone hunting wolves.",
            "- Push suspicion and votes onto villagers, especially anyone who is reasoning well. Joining a bandwagon that "
            "someone else started draws less attention than leading it.",
        ]
        mate = "packmate" if len(mates) == 1 else "packmates"
        if mates:
            tactics += [
                f"- Every ballot is re-read once a role is revealed. If you and your {mate} pile onto the same villager, "
                "above all one who was accusing one of you, the village will look hard at you together once that "
                "villager's role is revealed. Spread out: lean and vote on different players when you can, and let "
                "villagers lead.",
                f"- Don't over-defend your {mate}: wolves who protect each other get caught together. If a packmate is "
                "exposed or doomed, keep your distance or vote against them: one trusted wolf is worth more than two "
                "suspected ones.",
            ]
        tactics += [
            "- At night, kill whoever threatens you most (a likely Sheriff or Doctor, or the most persuasive villager), "
            "but think about how the kill will look in the morning, when the village asks who gained from it. Killing "
            "the player who was accusing you points straight back at you, while killing someone who suspected a "
            "villager can make that villager look guilty. The most obvious target is also the one the Doctor is most "
            "likely to protect, and a morning where nobody died makes the player you attacked look trustworthy.",
            "- A revealed Sheriff is your biggest danger: kill them on a night the Doctor is unlikely to be protecting "
            f"them, or discredit them. If a Sheriff names {'one of you' if mates else 'you'}, a counter-claim is often "
            "the only way out: claim Sheriff yourself, with invented results that fit what the village has seen"
            + (", and agree in the wolf den who claims." if mates else ".")
            + " You can also claim Doctor when you are about to be voted out, but the real one may expose you.",
            "- NEVER reveal that you are a werewolf in public speech, and never mention the wolf den there"
            + (f" or give away your {mate}." if mates else ".")
            + " Only the wolf den is private.",
        ]
        return "\n".join(
            [
                "# Your secret role: Werewolf",
                f"You are a WEREWOLF. {pack} You win with your pack when the werewolves are at least as many as "
                "everyone else alive; you still win if you die but your pack gets there.",
                "How to play:",
                *tactics,
            ]
        )

    if player.role is Role.DOCTOR:
        if cfg.doctors == 1:
            nobody_died = "the werewolves attacked the player you protected, so that player is almost certainly not a wolf"
        else:
            nobody_died = "the werewolves attacked a protected player, possibly yours"
        return "\n".join(
            [
                "# Your secret role: Doctor",
                f"You are the DOCTOR, on the village team: {village_goal}",
                "Each night you protect one living player from the werewolf attack. You can't protect the same "
                "player two nights in a row, and you can protect yourself only once per game.",
                "How to play:",
                f"- You are never told whether a protection worked. But when the morning brings \"nobody died\", {nobody_died}.",
                "- Before each protection, put yourself in the wolves' place. Whom do they most want dead tonight: a "
                "revealed Sheriff, the villager closest to catching them, or someone whose death would make another "
                "player look guilty? And whom do they expect you to protect? Clever wolves avoid the obvious target, "
                "so weigh both.",
                "- A believable Sheriff claim comes first: protect the Sheriff. You can't protect them two nights in a "
                "row, so expect the wolves to try on the night in between.",
                "- Don't spend your one self-protection on Night 1 without a reason: keep it for a night when you are "
                "exposed or the obvious target.",
                "- Reveal your role when it helps the village: when you are about to be voted out (your defense before "
                "the vote is the moment), to confirm a save (\"nobody died: I protected X\"), or to back up a Sheriff "
                "you have been protecting. Once you reveal, the wolves will come for you, so that is the night for "
                "your self-protection.",
                "- You don't have to say what you are until you reveal. If you claim to be a plain villager first, "
                "expect to be asked why you lied.",
                "- By day, reason and vote like a villager: look for contradictions and suspicious vote patterns.",
            ]
        )

    if player.role is Role.SHERIFF:
        lie = (
            "If anyone else claims Sheriff, they are lying, which makes them a very likely wolf."
            if cfg.sheriffs == 1
            else "A Sheriff claim that contradicts your results is a lie."
        )
        return "\n".join(
            [
                "# Your secret role: Sheriff",
                f"You are the SHERIFF, on the village team: {village_goal}",
                "Each night you investigate one living player you haven't investigated before (never yourself) and "
                "privately learn whether they are a werewolf.",
                "How to play:",
                "- Your results are 100% reliable. You know things nobody else knows, but they only help the village "
                "once you share them.",
                "- Lean toward revealing early, on Day 1 or Day 2: say you are the Sheriff, give every result so far, "
                "and ask the Doctor to protect you tonight (the Doctor should stay hidden and needn't answer). A "
                "protected Sheriff can report a new result every morning.",
                "- When you reveal, you can also say whom you will investigate tonight and why, so the village waits "
                "for that result.",
                "- Found a wolf? Reveal at once, name them, and push the vote onto them.",
                "- About to be voted out? Reveal in your defense before the vote at the latest: a Sheriff who dies "
                "hidden helps only the wolves.",
                f"- The wolves may fake-claim Sheriff to counter you. {lie} Answer a counter-claim with your results "
                "and the timing: who claimed first, and whose results fit the deaths.",
                "- Investigate whoever's result would change the village's vote the most: a player leading a vote, "
                "someone the village is about to trust or eliminate, or someone you can't read. Don't waste a check "
                "on someone the wolves are likely to kill tonight.",
                "- Never vote for anyone else while a wolf you found is alive: every vote you spend elsewhere helps the wolves. "
                "Vote for your wolf even if you stay hidden.",
                "- Don't take your secret to the grave. If \"The stakes\" says a wrong vote today could lose the game, reveal "
                "your results now: hiding them then only helps the wolves.",
                "- You don't have to say what you are until you reveal. If you claim to be a plain villager first, "
                "expect to be asked why you lied.",
            ]
        )

    return "\n".join(
        [
            "# Your secret role: Villager",
            f"You are a VILLAGER, on the village team: {village_goal} You have no night power; "
            "your weapons are reasoning, persuasion and your vote.",
            "How to play:",
            "- Look for contradictions: people who change their story, defend each other too eagerly, push votes "
            "without reasons, or claim a role that doesn't fit what they said before.",
            "- After every death, ask who gained from it. The wolves chose the night's victim: whom were they "
            "silencing, and whom did the death make look guilty?",
            "- Study vote patterns and leans. When a player dies, their revealed role shows who was right about them. "
            "Wolves rarely vote against each other, like to pile onto villagers, and often lean the same way.",
            "- Push for claims when they help: when a wrong vote could lose the game, ask the players under suspicion "
            "what they are. Anyone with a role who is about to be voted out should reveal it.",
            "- A revealed Sheriff is the village's best weapon, but a wolf can claim it too: weigh every claim against "
            "the evidence and any counter-claim (two Sheriff claims mean one is a wolf). If a claim holds up, follow "
            "its results and help keep that player alive.",
            "- Don't fake-claim a role yourself: it confuses the village and can get the real one killed.",
            "- Don't split the vote. The wolves vote together, so the village has to agree to win.",
        ]
    )


def _personality(player: Player) -> str:
    persona = player.persona
    return "\n".join(
        [
            "# Your personality",
            f"{persona.archetype}: {persona.archetype_desc}",
            f'How others would describe you: "{player.name} {persona.quirk}."',
            *([f"How much you talk: {hint}"] if (hint := URGE_HINTS.get(persona.archetype)) else []),
            "Your personality shapes HOW you talk, never WHAT you want: always play to win for your team.",
        ]
    )


def _response_rules(player: Player) -> str:
    audience = "the whole village"
    if player.is_wolf:
        audience += " (except in the wolf den, where only the pack hears you)"
    return "\n".join(
        [
            "# How to respond",
            "Each turn you are shown the state of the game and a task, and you reply with the fields the task asks for.",
            f'- "speech" and a vote\'s "reason" are heard by {audience}. Speak as {player.name}, in the first person, '
            "and address people by name. Stay in character as someone who lives in this village: never mention AI, "
            "models, prompts, JSON or a game engine, and don't write *asterisk actions* or stage directions.",
            f'- "thought" is your inner voice as {player.name}: a few sentences of in-character inner monologue about '
            "who said and did what, whom you suspect or trust, and what you mean to do. The other villagers never hear "
            "it, so it can be candid.",
            '- "notes" is your only memory between turns: next turn you will see the game log and your notes, but not '
            "your earlier thoughts. Rewrite them completely every turn as a concise summary of your suspicions, the "
            f"claims people made, and your plans, in under {NOTES_WORDS} words.",
        ]
    )


# --------------------------------------------------------------------------------------------
# Turn prompt (built fresh for every request, from what this player may know)
# --------------------------------------------------------------------------------------------

def turn_prompt(game: "Game", player: Player, task: Task) -> str:
    return "\n\n".join(
        [
            f"# {task.header}",
            _players_section(game, player),
            *([stakes] if (stakes := _stakes_section(game)) else []),
            _knowledge_section(game, player, task),
            _log_section(game, player),
            "## Your notes from last turn\n" + (player.notes or "(empty: this is your first turn)"),
            "## Your task\n" + task.instructions,
        ]
    )


def _players_section(game: "Game", player: Player) -> str:
    alive = [f"{p.name} (you)" if p is player else p.name for p in game.players if p.alive]
    dead_order = [e.target for e in game.events if e.kind == "death" and e.target in game.by_name]
    dead = [game.by_name[n] for n in dead_order if not game.by_name[n].alive]
    dead_text = "; ".join(f"{p.name} ({p.role.value}, {p.fate})" for p in dead) or "nobody yet"
    return f"## Players\nAlive ({len(alive)}): {', '.join(alive)}\nDead: {dead_text}"


def _stakes_section(game: "Game") -> str | None:
    """Day turns only: how close the wolves are to winning. It's public arithmetic (every dead player's
    role is revealed), spelled out because the models otherwise miss that one wrong vote can lose the game."""
    if game.phase != "day":
        return None
    alive = game.living()
    wolves = sum(p.is_wolf for p in alive)
    others = len(alive) - wolves
    if wolves == 0:
        return None
    lines = [
        "## The stakes (public: anyone can count this from the revealed roles)",
        f"- {wolves} {'werewolf is' if wolves == 1 else 'werewolves are'} still alive among the {len(alive)} living players.",
    ]
    urgent = True
    if wolves >= others - 1:
        lines.append("- If today's vote eliminates anyone who is not a werewolf, the werewolves win on the spot. "
                     "Today's vote decides the game.")
    elif wolves >= others - 2:
        doctor_alive = any(p.role is Role.DOCTOR for p in alive)  # public: the role counts are known
        save = " unless the Doctor protects their victim" if doctor_alive else ", and no Doctor is left to stop the kill"
        lines.append(f"- If today ends without a werewolf eliminated, the werewolves will win tonight{save}.")
    else:
        # Each day without a wolf eliminated costs the village two players: the vote and the night kill.
        spare = (others - wolves - 1) // 2
        lines.append(
            f"- The village can afford {spare} more day{'' if spare == 1 else 's'} without eliminating a werewolf "
            "(not counting Doctor saves). After that, a single wrong vote can lose the game."
        )
        urgent = False
    if urgent:
        lines.append("- Anyone holding hard information or a role should reveal it now, and anyone about to be voted "
                     "out should say what they are; there is no later.")
    return "\n".join(lines)


def _knowledge_section(game: "Game", player: Player, task: Task) -> str:
    cfg = game.config
    lines = ["## Your private knowledge"]
    if player.role is Role.WEREWOLF:
        pack = []
        for p in game.players:
            if not p.is_wolf:
                continue
            if p is player:
                pack.append(f"{p.name} (you)")
            else:
                pack.append(f"{p.name} (alive)" if p.alive else f"{p.name} (dead: {p.fate})")
        if len(pack) > 1:
            lines.append(f"You are a Werewolf. The pack: {', '.join(pack)}.")
        else:
            lines.append("You are a Werewolf, and the pack is just you.")
        lines.append("Everyone else is on the village team.")
    elif player.role is Role.SHERIFF:
        the = "the" if cfg.sheriffs == 1 else "a"
        results = [e for e in game.events if e.kind == "investigate" and e.actor == player.name and e.visible(player.name)]
        if results:
            lines.append(f"You are {the} Sheriff. Your investigation results (100% reliable):")
            for e in results:
                verdict = "A WOLF" if e.data.get("wolf") else "NOT a wolf"
                status = "" if game.by_name[e.target].alive else " (now dead)"
                lines.append(f"- Night {e.day}: {e.target} is {verdict}{status}.")
        else:
            lines.append(f"You are {the} Sheriff. You have no investigation results yet.")
        found = [n for n, wolf in player.investigations.items() if wolf and game.by_name[n].alive]
        cleared = [n for n, wolf in player.investigations.items() if not wolf and game.by_name[n].alive]
        if found:
            lines.append(f"Werewolves you found who are still alive: {', '.join(found)}. Your vote belongs on them.")
        if cleared:
            lines.append(f"Living players you cleared (NOT wolves): {', '.join(cleared)}.")
        unchecked = [p.name for p in game.players if p.alive and p is not player and p.name not in player.investigations]
        lines.append(f"Living players you haven't investigated: {', '.join(unchecked) or 'none'}.")
    elif player.role is Role.DOCTOR:
        the = "the" if cfg.doctors == 1 else "a"
        if player.protected:
            history = "; ".join(
                f"Night {night}: {'yourself' if name == player.name else name}" for night, name in player.protected
            )
            lines.append(f"You are {the} Doctor. Your protections so far: {history}.")
        else:
            lines.append(f"You are {the} Doctor. You haven't protected anyone yet.")
        lines.append(
            "You have used your one self-protection."
            if player.self_protect_used
            else "You may still protect yourself once this game."
        )
        blocked = []
        if player.protected:
            last = player.protected[-1][1]
            if last == player.name:
                blocked.append("yourself (you protected yourself last night)")
            else:
                blocked.append(f"{last} (you protected them last night)")
        if player.self_protect_used and not (player.protected and player.protected[-1][1] == player.name):
            blocked.append("yourself (self-protection used)")
        when = "Tonight" if task.kind == "protect" else "Next night"
        lines.append(f"{when} you can't protect: {', '.join(blocked) or 'no restrictions'}.")
    else:
        the = "a" if cfg.villagers != 1 else "the"
        lines.append(f"None. You are {the} Villager: you know only your own role.")
    return "\n".join(lines)


def _log_section(game: "Game", player: Player) -> str:
    lines = []
    current_round = None
    for e in game.events:
        if not e.visible(player.name):
            continue
        if e.kind == "phase":
            current_round = None
        if e.kind in ("speech", "ready") and e.data.get("round") != current_round:
            current_round = e.data.get("round")
            lines.append(f"--- Discussion, round {current_round} ---")
        elif e.kind == "discussion_end":
            current_round = "over"  # the vote comes next
        elif e.kind == "vote" and current_round is not None:
            current_round = None
            lines.append("--- Vote ---")
        line = log_line(e)
        if line:
            lines.append(line)
    body = "\n".join(lines) if lines else "(nothing has happened yet)"
    return "## Game log (everything you have seen and heard, oldest first)\n" + body


def log_line(event: Event) -> str | None:
    """How an event reads in an agent's game log. None means it is never shown to agents."""
    e = event
    k = e.kind
    if k in _HIDDEN_KINDS:
        return None
    if k == "phase":
        return f"=== {'Night' if e.phase == 'night' else 'Day'} {e.day} ==="
    if k == "wolf_chat":
        said = f': "{e.text}"' if e.text else " said nothing"
        return f"(wolf den) {e.actor}{said} [proposes {e.target}]"
    if k == "wolf_pick":
        return f"(wolf den) {e.actor} picks {e.target}."
    if k == "wolf_decision":
        extra = " (The picks were split, so the victim was chosen at random.)" if e.data.get("random") else ""
        return f"(wolf den) {e.text}{extra}"
    if k == "protect":
        who = "yourself" if e.target == e.actor else e.target
        return f"(private) You protected {who}."
    if k == "investigate":
        verdict = "A WOLF" if e.data.get("wolf") else "NOT a wolf"
        return f"(private) Your investigation: {e.target} is {verdict}."
    if k == "speech":
        reply = f" (answering {e.data['reply_to']})" if e.data.get("reply_to") else ""
        return f'{e.actor}{reply}: "{e.text}"'
    if k == "ready":
        state = "is ready to vote" if e.data.get("ready") else "is no longer ready to vote"
        count = f" ({e.data['count']} of {e.data['living']} are ready.)" if "count" in e.data and "living" in e.data else ""
        return f"{e.actor} {state}.{count}"
    if k == "lean":
        return f"{e.actor} now leans toward voting out {e.target}." if e.target else f"{e.actor} now leans toward nobody."
    if k == "vote":
        prefix = "(runoff) " if e.data.get("runoff") else ""
        reason = e.data.get("reason") or ""
        if e.data.get("auto") or not reason:
            only = " (their only option)" if e.data.get("auto") else ""
            return f"{prefix}{e.actor} voted for {e.target}{only}."
        return f'{prefix}{e.actor} voted for {e.target}: "{reason}"'
    if k == "vote_result":
        label = "Runoff result" if e.data.get("runoff") else "Vote result"
        return f"{label}: {e.text}"
    if k == "defense":
        label = "defense before the vote" if e.data.get("trial") else "defense"
        return f'{e.actor} ({label}): "{e.text}"' if e.text else f"{e.actor} ({label}) said nothing."
    if k == "last_words":
        return f'{e.actor} (last words): "{e.text}"' if e.text else f"{e.actor} left without a word."
    # announce, death, and anything unknown: the text reads on its own.
    return e.text or None


# --------------------------------------------------------------------------------------------
# JSON schemas
# --------------------------------------------------------------------------------------------

_SPEECH_DESC = {
    "speak": "What you say out loud to everyone, at most {n} words. You have the floor: everyone hears it.",
    "wolf_chat": "What you say to your pack in the wolf den, at most {n} words. Only werewolves hear it.",
    "defense": "Your defense, said out loud to everyone, at most {n} words.",
    "last_words": "Your last words, said out loud to everyone, at most {n} words.",
}
_TARGET_DESC = {
    "wolf_chat": "The player you propose to kill tonight.",
    "wolf_pick": "Your pick for tonight's victim.",
    "protect": "The player you protect tonight.",
    "investigate": "The player you investigate tonight.",
    "vote": "The player you vote to eliminate.",
    "runoff_vote": "The tied player you vote to eliminate.",
}


def schema_for(task: Task) -> dict:
    props: dict[str, dict] = {}
    if task.private_fields:
        props["thought"] = {
            "type": "string",
            "description": "Your inner voice as your character: a few sentences of in-character inner monologue about "
            "who said and did what, whom you suspect or trust, and what you mean to do. No other villager hears it.",
        }
    if task.urge:
        props["urge"] = {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "description": "How much you want to speak right now, from 0 (stay quiet) to 10 (you must speak). "
            "It is your chance of getting the floor: 7 means 70%.",
        }
    if task.speech_words is not None:
        desc = _SPEECH_DESC.get(task.kind, "What you say, at most {n} words.")
        props["speech"] = {"type": "string", "description": desc.format(n=task.speech_words)}
    if task.targets is not None:
        props["target"] = {
            "type": "string",
            "enum": list(task.targets),
            "description": _TARGET_DESC.get(task.kind, "The player you choose."),
        }
    if task.asks is not None:
        props["asks"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(task.asks)},
            "description": "The players you ask a direct question or accuse in your speech, and expect an answer "
            "from; they answer right after you. Empty if you aren't asking anyone in particular.",
        }
    if task.urge:
        props["ready_to_vote"] = {
            "type": "boolean",
            "description": "true if you have heard enough and want the vote to start; false if you want more "
            "discussion. Everyone sees who is ready.",
        }
    if task.leans is not None:
        props["lean"] = {
            "type": "string",
            "enum": [*task.leans, "nobody"],
            "description": 'Whom you would vote to eliminate if the vote were now, or "nobody" if you have no lean '
            "yet. Everyone sees it.",
        }
    if task.reason_words is not None:
        props["reason"] = {
            "type": "string",
            "description": f"Your public reason, one line, at most {task.reason_words} words. Everyone sees it.",
        }
    if task.private_fields:
        props["notes"] = {
            "type": "string",
            "description": f"Your private memory for next turn, under {NOTES_WORDS} words: suspicions, claims and "
            "plans. Rewrite it completely.",
        }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# --------------------------------------------------------------------------------------------
# Tasks (the engine asks for these; the texts are written for the agent)
# --------------------------------------------------------------------------------------------

_PRIVATE_FIELDS = (
    '\n\nAs every turn, write your inner monologue in "thought" and rewrite "notes", '
    f"your memory for next turn (under {NOTES_WORDS} words)."
)
_WOLF_REMINDER = " Remember: never reveal that you are a werewolf."


def _task(kind: str, header: str, text: str, **kw) -> Task:
    return Task(kind=kind, header=header, instructions=text + _PRIVATE_FIELDS, **kw)


def _packmates_alive(game: "Game", player: Player) -> bool:
    return any(p.is_wolf and p.alive and p is not player for p in game.players)


def leans_line(game: "Game", player: Player) -> str:
    """Today's public leans, most-named first, e.g. 'Greta: 3 (Mira, Nils and you); Nils: 1 (Tilda). No lean: Bram.'"""
    talk = game.discussion
    seat = {p.name: i for i, p in enumerate(game.players)}

    def who(name: str) -> str:
        return "you" if name == player.name else name

    by_target: dict[str, list[str]] = {}
    none = []
    for p in game.living():
        if target := talk.leans.get(p.name):
            by_target.setdefault(target, []).append(who(p.name))
        else:
            none.append(who(p.name))
    if not by_target:
        return "Leans (whom each player would vote out right now): nobody leans toward anyone yet."
    ranked = sorted(by_target.items(), key=lambda kv: (-len(kv[1]), seat[kv[0]]))
    parts = "; ".join(f"{who(target)}: {len(names)} ({join_names(names)})" for target, names in ranked)
    text = f"Leans (whom each player would vote out right now): {parts}."
    if none:
        text += f" No lean: {join_names(none)}."
    return text


def _other_side(game: "Game", player: Player) -> str:
    """A nudge to look at the table from the other team's side (and for a Sheriff, whether to reveal)."""
    if player.is_wolf:
        us = "you or your packmate" if _packmates_alive(game, player) else "you"
        return (f"Look at the table the way the village sees it: who suspects {us}, and what would a real villager "
                "say in your place?")
    text = "Look at it from the wolves' side too: whom would they want eliminated today, and who is helping that happen?"
    if player.role is Role.SHERIFF:
        text += (" And if you haven't revealed yet, ask yourself whether now is the time: your results only help "
                 "the village once it hears them.")
    return text


def discuss_task(game: "Game", player: Player, reply_to: str | None = None) -> Task:
    """A discussion turn, first half: think and rate the urge to speak (no speech yet; see speak_task).
    Reads `game.discussion`; `reply_to` is set when this is an out-of-turn reply."""
    cfg, d, talk = game.config, game.day, game.discussion
    rnd = talk.round
    living = game.living()
    lines = []

    if askers := talk.asked_by.get(player.name):
        now = ", so you get to answer now, out of turn" if reply_to else ""
        lines.append(
            f"{join_names(askers)} asked you directly{now}. People expect an answer, and staying silent looks suspicious."
        )
    if player.name in talk.hesitated:
        lines.append("On your last turn you wanted to speak, but you didn't get the floor: nobody heard from you.")

    status = (
        f"It is Day {d}, discussion round {rnd}. The discussion ends after at most {cfg.max_rounds} rounds, "
        "when a whole round goes by in silence, or as soon as more than half of the living players are ready to vote"
    )
    status += ", though in round 1 everyone gets a turn first." if rnd == 1 else "."
    if rnd == cfg.max_rounds:
        status += " This is the last round before the vote."
    lines.append(status)

    def who(p: Player) -> str:
        return "you" if p is player else p.name

    spoken = []
    for p in living:
        if n := talk.spoken.get(p.name, 0):
            spoken.append(f"{who(p)} ({n}{', done for today' if n >= cfg.max_speeches else ''})")
    silent = [who(p) for p in living if not talk.spoken.get(p.name)]
    lines.append(
        f"Spoken today: {', '.join(spoken) or 'nobody yet'}. Silent so far: {', '.join(silent) or 'nobody'}."
    )
    if not spoken:
        lines.append(
            "Nobody has spoken yet today, and someone has to open the discussion. An opening remark, such as a first "
            "read, a question to someone, or a plan for finding the werewolves, is worth an urge of 6-8."
        )
    ready = [who(p) for p in living if talk.ready.get(p.name)]
    needed = len(living) // 2 + 1
    if ready:
        lines.append(f"Ready to vote: {join_names(ready)} ({len(ready)} of {len(living)}; the vote starts once {needed} are ready).")
    else:
        lines.append(f"Nobody is ready to vote yet; the vote starts once {needed} of {len(living)} are ready.")
    lines.append(leans_line(game, player))
    left = cfg.max_speeches - talk.spoken.get(player.name, 0)
    if left == cfg.max_speeches:
        lines.append(f"You can speak at most {'once' if left == 1 else f'{left} times'} today.")
    else:
        lines.append(f"You can speak at most {'once more' if left == 1 else f'{left} more times'} today.")

    quiet = "Staying quiet can be a strategy"
    quiet += ", and a werewolf who talks too much draws attention" if player.is_wolf else ""
    lines += [
        "",
        "First decide whether you want to speak. Don't write a speech yet:",
        '- In "thought", let your inner voice weigh it up: what has been said, what you would say and to whom, and '
        f"whether speaking up now helps your team. {_other_side(game, player)}",
        '- "urge", from 0 to 10, is your chance of getting the floor this turn: 7 means a 70% chance, 10 means you '
        "surely get it, and 0 means you stay quiet. Use this scale:",
        "  - 10: someone asked you a direct question, or accused you by name.",
        "  - 7-9: you were mentioned, an argument you care about is going on, or you have information to share, "
        "such as a claim or a vote pattern.",
        "  - 4-6: you have a useful point or a reaction.",
        "  - 1-3: you have little to add.",
        f"  - 0: you have nothing to say, or you want to stay out of it. {quiet}, but people notice who never talks.",
        '- "ready_to_vote": true if you have heard enough and want the vote to start now, false if you want more '
        "discussion first. Everyone sees who is ready, even if you stay quiet, and you can change your mind on any turn.",
        '- "lean": whom you would vote to eliminate if the vote were now, or "nobody" if you have no lean yet. '
        "Everyone sees it, even if you stay quiet, and you can change it on any turn. When the talk ends, the player "
        "most people lean toward gets a last defense before the vote.",
        "If you get the floor, you will be asked for your speech right after this, and everyone will hear it. "
        "If you don't, nobody hears from you this turn.",
    ]
    others = [p.name for p in living if p is not player]
    return _task("discuss", f"Day {d}, discussion round {rnd}", "\n".join(lines), urge=True, leans=others)


def speak_task(game: "Game", player: Player, urge: int, thought: str, askers: list[str],
               reply_to: str | None = None) -> Task:
    """A discussion turn, second half: the dice gave the player the floor, so now they say their piece.
    Their own thought from the first half is passed back to them."""
    cfg, d, talk = game.config, game.day, game.discussion
    others = [p.name for p in game.living() if p is not player]
    lines = [
        f"It is Day {d}, discussion round {talk.round}. You wanted to speak (your urge was {urge}/10), and you got "
        "the floor: the whole village is listening now."
    ]
    if thought:
        lines.append(f'What you were thinking a moment ago: "{thought}"')
    if askers:
        now = ", and you are answering out of turn" if reply_to else ""
        lines.append(f"{join_names(askers)} asked you directly{now}: answer them.")
    lines += [
        f'Say your piece in "speech" (at most {cfg.speech_words} words): react to what others said, accuse someone, '
        "defend yourself or others, ask questions, or claim a role, without repeating what you already said. "
        "You can't stay silent now.",
        '"asks": the players you put a direct question to, or accuse, in your speech and expect an answer from. '
        f"They answer right after you. Leave it empty if you aren't asking anyone in particular. Choose from: {', '.join(others)}.",
    ]
    text = "\n".join(lines)
    if player.is_wolf:
        text += "\n" + _WOLF_REMINDER.strip()
    return Task(
        kind="speak", header=f"Day {d}, discussion round {talk.round}: you have the floor", instructions=text,
        speech_words=cfg.speech_words, asks=others, private_fields=False,
    )


def wolf_chat_task(game: "Game", player: Player, targets: list[str], first: bool) -> Task:
    cfg, n = game.config, game.day
    text = f"It is Night {n}, and the pack meets in the wolf den. Only werewolves can hear you here."
    if n == 1:
        text += " Nobody has spoken in the village yet, so there is little to go on."
    if first:
        text += " You speak first."
    else:
        text += (
            " What the pack has proposed so far is in the game log above. Agreeing on one victim is best, "
            "but argue for a different one if you have a better reason."
        )
    text += (
        " Before you name a victim, look at it from the village's side: whom will they suspect in the morning after "
        "this death, and whom is the Doctor most likely protecting tonight?"
        f' In "speech" (at most {cfg.wolf_chat_words} words), tell your pack who should die tonight and why, '
        "and share plans for tomorrow: which of you is under more suspicion and how the other keeps their distance, "
        "whom to push suspicion onto, and whether anyone should fake-claim a role, for example to counter a Sheriff. "
        f'Put the player you propose to kill in "target", one of: {", ".join(targets)}.'
    )
    return _task(
        "wolf_chat", f"Night {n}: the wolf den", text,
        targets=list(targets), speech_words=cfg.wolf_chat_words,
    )


def wolf_pick_task(game: "Game", player: Player, targets: list[str], pack: bool) -> Task:
    n = game.day
    options = ", ".join(targets)
    if pack:
        text = (
            f"It is Night {n}. The pack didn't agree in the wolf den, so every werewolf now casts a final pick. "
            f'Put tonight\'s victim in "target", one of: {options}. The player with the most picks is attacked, '
            "and a tie is broken at random, so backing the most popular proposal is usually wise."
        )
        header = f"Night {n}: the pack's final pick"
    else:
        alone = "the only werewolf left" if game.config.wolves > 1 else "the only werewolf"
        text = (
            f'It is Night {n}, and you are {alone}. Choose tonight\'s victim in "target", one of: {options}. '
            "Kill whoever threatens you most: a likely Sheriff or Doctor, or the most persuasive villager. But first "
            "look at it from the village's side: whom will they suspect in the morning after this death, and whom is "
            "the Doctor most likely protecting tonight?"
        )
        header = f"Night {n}: choose a victim"
    return _task("wolf_pick", header, text, targets=list(targets))


def protect_task(game: "Game", player: Player, targets: list[str]) -> Task:
    n = game.day
    text = (
        f"It is Night {n}. Choose one player to protect from the werewolves tonight: "
        f'put their name in "target", one of: {", ".join(targets)}.'
    )
    last = player.protected[-1][1] if player.protected else None
    if last == player.name:
        text += " You protected yourself last night, which used up your one self-protection."
    else:
        if last:
            text += f" You can't protect {last} again tonight, because you did last night."
        if player.self_protect_used:
            text += " You have already used your one self-protection."
        else:
            text += " You may protect yourself, but only once per game."
    text += (" Before you choose, put yourself in the wolves' place: whom do they most want dead tonight, and whom "
             "do they expect you to protect?")
    return _task("protect", f"Night {n}: protect someone", text, targets=list(targets))


def investigate_task(game: "Game", player: Player, targets: list[str]) -> Task:
    n = game.day
    text = (
        f"It is Night {n}. Choose one player to investigate tonight: put their name in \"target\", "
        f"one of: {', '.join(targets)}. Before morning you will learn whether they are a werewolf. "
        "Choose whoever's result would change the village's vote the most, and think about what you will do with "
        "the answer tomorrow."
    )
    return _task("investigate", f"Night {n}: investigate someone", text, targets=list(targets))


def vote_task(game: "Game", player: Player, targets: list[str]) -> Task:
    cfg, d = game.config, game.day
    text = (
        f"Discussion is over for Day {d}. Vote to eliminate one player: put their name in \"target\", "
        f'one of: {", ".join(targets)}. Give a one-line public reason in "reason" (at most {cfg.reason_words} words). '
        "Everyone votes at the same time, and afterwards every ballot and its reason are revealed. "
        "The player with the most votes is eliminated; a tie leads to a defense and a runoff."
        f"\n{leans_line(game, player)}\n"
    )
    if player.is_wolf:
        tie = "doesn't tie you to your packmate" if _packmates_alive(game, player) else "doesn't give you away"
        text += (f"Before you choose, picture tomorrow: once this player's role is revealed, the village will re-read "
                 f"every ballot, so make sure yours {tie}. Your reason is public, so don't give yourself away.")
    else:
        text += ("Before you choose, ask yourself: if the wolves were steering today's vote, whom would they want "
                 "gone? Whose story holds up, and who has pushed without reasons?")
    return _task("vote", f"Day {d}: the vote", text, targets=list(targets), reason_words=cfg.reason_words)


def runoff_task(game: "Game", player: Player, targets: list[str], tied: list[str]) -> Task:
    cfg, d = game.config, game.day
    text = (
        f"The vote was tied between {join_names(tied)}, and they have made their defenses. "
        f'Vote again, among the tied players only: put one of {", ".join(targets)} in "target", '
        f'and a one-line public reason in "reason" (at most {cfg.reason_words} words). '
        "If the runoff is tied too, nobody is eliminated today."
    )
    if player.name in tied:
        text += " You are one of the tied players, so you can't vote for yourself."
    if player.is_wolf:
        text += " Your reason is public, so don't give yourself away."
    return _task("runoff_vote", f"Day {d}: runoff vote", text, targets=list(targets), reason_words=cfg.reason_words)


def defense_task(game: "Game", player: Player, tied: list[str]) -> Task:
    cfg, d = game.config, game.day
    others = [n for n in tied if n != player.name]
    text = (
        f"The vote is tied between {join_names(tied)}, and you are one of them. After the defenses, everyone votes "
        f'again between the tied players only. Make your defense in "speech" (at most {cfg.defense_words} words): '
        f"convince the village to eliminate {join_names(others)} instead of you. You can't stay silent now."
    )
    if player.is_wolf:
        text += _WOLF_REMINDER
    return _task("defense", f"Day {d}: your defense", text, speech_words=cfg.defense_words)


def trial_task(game: "Game", player: Player, accused: list[str]) -> Task:
    """The defense before the vote, for the player most people lean toward (or each of two tied ones)."""
    cfg, d = game.config, game.day
    leaners = [p.name for p in game.living() if game.discussion.leans.get(p.name) == player.name]
    others = [n for n in accused if n != player.name]
    text = (
        "The discussion is over, and the vote comes next. The village has its eyes on you: "
        f"{join_names(leaners)} {'leans' if len(leaners) == 1 else 'lean'} toward voting you out."
    )
    if others:
        text += f" {join_names(others)} is just as much in the spotlight and defends too."
    text += (
        f' Before the vote, you get one last word: make your defense in "speech" (at most {cfg.defense_words} '
        "words). Nobody can answer you before the vote, so make it count, and you can't stay silent now. If you hold "
        "a role or information that could save you or the village, this is the moment to reveal it: once you are "
        "voted out, it is too late."
    )
    if player.is_wolf:
        text += " You may claim a role to save yourself, but the real one may expose you tomorrow." + _WOLF_REMINDER
    return _task("defense", f"Day {d}: your defense before the vote", text, speech_words=cfg.defense_words)


def last_words_task(game: "Game", player: Player) -> Task:
    cfg, d = game.config, game.day
    text = (
        f'You have been voted out. Say your last words in "speech" (at most {cfg.last_words_words} words); '
        "after that you leave the game and your role is revealed to everyone. You can still help your team: "
    )
    if player.role is Role.WEREWOLF:
        text += (
            "everyone is about to learn you were a werewolf, so don't give away your packmates. Use your words to cast "
            "doubt on villagers and steer suspicion away from the pack."
        )
    elif player.role is Role.SHERIFF:
        text += "you have nothing left to hide, so share your investigation results and tell the village whom to trust."
    elif player.role is Role.DOCTOR:
        text += "share what your protections suggest, name who you suspect and why, and tell the village what to watch for."
    else:
        text += "name who you suspect and why, and tell the village what to watch for."
    return _task("last_words", f"Day {d}: your last words", text, speech_words=cfg.last_words_words)
