import random
from dataclasses import dataclass

# Distinct first letters keep the chat easy to scan.
NAMES = [
    "Alice", "Bram", "Clara", "Dmitri", "Elena", "Finn", "Greta", "Hugo", "Iris", "Jonas",
    "Katya", "Leo", "Mira", "Nils", "Olga", "Pavel", "Rosa", "Silas", "Tilda", "Viktor",
]

ARCHETYPES = {
    "Detective": "Methodical and logical. You cite specific evidence (who said what, who voted how) and ask pointed questions.",
    "Paranoid": "Nervous and suspicious of everyone. You accuse quickly and read hidden meaning into small things.",
    "Peacemaker": "Warm and diplomatic. You calm heated arguments, look for consensus, and are slow to accuse.",
    "Loudmouth": "Dramatic, emotional and theatrical. You talk with exclamation marks and strong opinions.",
    "Quiet One": "Reserved and terse. You speak in short sentences, observe more than you talk, and choose words carefully.",
    "Joker": "Sarcastic and witty. You tease and joke even when things are serious, but you still make real points.",
    "Follower": "Agreeable. You tend to side with whoever sounds most convincing and often echo their points.",
    "Contrarian": "Skeptical of crowds. When everyone agrees, you push back and argue the other side.",
    "Village Elder": "Old, folksy and wise-sounding. You speak in proverbs and little stories from the village's past.",
    "Hothead": "Short-tempered. You take accusations personally and fire back hard.",
    "Gossip": "Loves rumors. You repeat what others said and speculate about who is secretly allied with whom.",
}

# How keen each archetype is to talk, for the discussion's 0-10 "urge". A hint only: the agent picks the number.
URGE_HINTS = {
    "Detective": "You speak up when you have evidence to cite or a question to ask; your urge is often 5-8, lower when you have nothing new.",
    "Paranoid": "You jump in whenever something looks suspicious; your urge is often 6-9.",
    "Peacemaker": "You speak up mostly to calm a fight or build agreement; your urge rises when people argue and drops when things are calm.",
    "Loudmouth": "You usually want to talk; your urge is often 6-10.",
    "Quiet One": "You rarely volunteer; your urge is usually 0-4, unless someone asks you directly.",
    "Joker": "You can't resist a quip, but you know when to let others talk; your urge is often 4-8.",
    "Follower": "You speak up mostly to agree with someone convincing; your urge is usually 2-6.",
    "Contrarian": "You speak up when the village agrees too quickly; your urge jumps when a consensus forms.",
    "Village Elder": "You speak when you have wisdom to share, not to fill silence; your urge is usually 3-6.",
    "Hothead": "You fire back the moment you are accused or mentioned (urge 9-10); otherwise your urge is moderate.",
    "Gossip": "You love to talk about who said what; your urge is often 6-9.",
}

# Third person, as other villagers would describe the player.
QUIRKS = [
    "gets defensive when accused",
    "always ends with a question",
    "gives other players nicknames",
    "holds grudges against anyone who suspected them",
    "is overconfident in their own reads",
    "keeps bringing up food",
    "loves counting votes and citing numbers",
    "speaks very formally and politely",
    "changes their mind easily",
    "quotes made-up old village sayings",
    "distrusts anyone who stays quiet",
    "is loyal to the first person who defended them",
]

JOBS = [
    "baker", "blacksmith", "farmer", "innkeeper", "fisher", "herbalist", "miller", "carpenter",
    "shepherd", "tailor", "weaver", "potter", "hunter", "candlemaker", "brewer",
]


@dataclass(frozen=True)
class Persona:
    name: str
    archetype: str
    archetype_desc: str
    quirk: str
    job: str


def _pick(pool: list, n: int, rng: random.Random) -> list:
    """n items from pool, unique while the pool lasts, then repeating."""
    out: list = []
    while len(out) < n:
        batch = pool[:]
        rng.shuffle(batch)
        out.extend(batch)
    return out[:n]


def deal_personas(n: int, rng: random.Random) -> list[Persona]:
    if n > len(NAMES):
        raise ValueError(f"at most {len(NAMES)} players are supported")
    names = rng.sample(NAMES, n)
    archetypes = _pick(list(ARCHETYPES), n, rng)
    quirks = _pick(QUIRKS, n, rng)
    jobs = _pick(JOBS, n, rng)
    return [
        Persona(name, arch, ARCHETYPES[arch], quirk, job)
        for name, arch, quirk, job in zip(names, archetypes, quirks, jobs)
    ]
