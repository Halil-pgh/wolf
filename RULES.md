# Wolf: How the Game Works

Wolf is a game of Werewolf (also called Mafia) in which every seat is taken by an AI player. A few
werewolves hide among the villagers. By night the wolves hunt. By day the whole village argues,
accuses and votes to eliminate someone they suspect. You watch it all unfold.

These are the rules the players are given, and the rules the game engine enforces.

---

## The players

A standard game has **9 players**, each dealt a secret role:

| Role | Icon | Count | Team | Power |
|---|:---:|:---:|---|---|
| **Werewolf** | 🐺 | 2 | Wolves | The wolves know each other. Each night they meet in secret and choose someone to kill. |
| **Doctor** | 🩺 | 1 | Village | Each night, protects one player from the wolves' attack. |
| **Sheriff** | ⭐ | 1 | Village | Each night, investigates one player and privately learns **"Wolf"** or **"Not a wolf"**. |
| **Villager** | 🌾 | 5 | Village | No special power. Villagers win through reasoning, persuasion and their votes. |

Every role is secret. The wolves know who their partner is; everyone else knows only their own role.

## How to win

The game checks for a winner after every death.

- **The village wins** as soon as every werewolf is dead.
- **The wolves win** as soon as the living wolves are at least as many as everyone else alive.
  For example, 2 wolves and 2 villagers is a wolf win, because the wolves can no longer be outvoted.
- **Draw:** if neither side has won by the end of **Day 10**, the game ends in a draw. This almost never happens.

A player who dies still wins or loses with their team.

---

## How a game flows

After roles, names and personalities are dealt, the game **starts at Night 1**. So Day 1 opens with
a death to discuss, and the Sheriff already holds one result.

The game then alternates between Night and Day until one side wins.

### 🌙 Night

Three things happen in secret during the night. The wolves, the Doctor and the Sheriff each act
without knowing what the others are doing.

**The wolves choose a victim.**
1. **The wolves' chat.** The wolves talk in their den, where only they can hear. Each living wolf
   speaks once, in a random order, and proposes a victim. Each wolf sees what was said before them.
2. **If they all proposed the same player**, that player is the target.
3. **If they disagree**, every wolf makes a final, secret pick.
4. **If the final picks are still split**, one of them is chosen at random.

The wolves must attack someone, and they can't attack a wolf. A lone wolf simply picks a victim.

**The Doctor protects one living player.** The Doctor has two limits:
- They can't protect the same player two nights in a row.
- They can protect themself **at most once per game**.

**The Sheriff investigates one living player** and privately learns "Wolf" or "Not a wolf". The
Sheriff can't investigate themself, and can't investigate the same player twice.

**At dawn:** if the wolves attacked the player the Doctor protected, nobody dies. Otherwise their target dies.

### ☀️ Day

1. **Morning news.** The village hears either *"X was killed in the night. X was the Doctor."* or
   *"Nobody died last night."* Nobody is told who was attacked or who was protected. If the death
   ends the game, it ends here.

2. **Discussion.** As in real life, not everyone talks all the time. The village talks in rounds,
   and in each round every living player gets a turn, in a fresh random order. On their turn a
   player rates how much they want to speak right now, an **urge from 0 to 10**, which is their
   chance of speaking: 7 means a 70% chance. Being asked a direct question or accused by name
   means 10; having nothing to add means 0. Only then are the dice rolled. A player who gets the
   floor says their piece, up to about **70 words**; one who doesn't stays quiet. Players accuse,
   defend, ask questions and form alliances. **Anyone may claim any role, and anyone may lie.**
   The wolves certainly will.
   - **Direct questions jump the queue.** Anyone asked a question or accused by name is asked
     next, out of turn. If they hadn't had their turn this round yet, that reply was it; if they
     had, it's an extra turn. After **3 replies in a row**, the round carries on as normal.
   - **Ready to vote.** On every turn, each player also says whether they have heard enough, and
     everyone sees who is ready, even when that player stays quiet.
   - **Who has talked.** Everyone can see who has spoken today, how often, and who has stayed
     silent. Nobody ever sees anyone's urge.
   - **The end.** The discussion ends as soon as one of these happens:
     - a whole round goes by in which nobody speaks;
     - more than half of the living players are ready to vote (but round 1 always finishes, so
       everyone gets a first chance to talk);
     - a safety cap is reached: at most **4 rounds** a day, at most **4 speeches** per player per day
       (replies included), and at most 5 turns per living player in all.

3. **Vote.** Everyone votes at the same time, without seeing anyone else's ballot. Each player must
   vote for one living player other than themself and give a one-line reason. **Nobody may abstain.**
   Then every ballot is revealed, for example *"Alice → Bram: dodged every question."*

4. **Result.** The player with the most votes is eliminated.
   - **On a tie**, each tied player gives a short defense, and then everyone votes again, choosing
     **only among the tied players**. A tied player can't vote for themself. If a player has only one
     legal choice, as a tied player does in a two-way tie, their vote is cast for them automatically.
   - **If the runoff is also tied**, nobody is eliminated that day.

5. **Last words.** The eliminated player makes a final statement of up to about 50 words, and then
   their role is revealed. Only players voted out by day get last words; night victims don't.

The game checks for a winner and moves on to the next Night.

---

## Who knows what

Hidden information is enforced by the game engine, not left to the players' good manners. Each
player's view is built only from what that player is allowed to see.

| | What they see |
|---|---|
| **Everyone** | Every day speech, who is ready to vote, every ballot and its reason, the morning news, defenses and last words, and the role of every dead player. |
| **Only you** | Your own role, your personality, and your own private notes. |
| **Wolves, also** | Who their partner is, and everything said in the wolves' chat. |
| **The Sheriff, also** | Their own investigation results. |
| **The Doctor, also** | Who they have protected, night by night. |
| **Nobody** | Anyone else's private thoughts, notes or urge to speak. |

Every day, each player is also reminded of **the stakes**: how many wolves are still alive, and how
many more days the village can afford without eliminating one. When the next wrong vote could lose
the game, the reminder says so. This is public arithmetic, since everyone can count the revealed
roles of the dead; it's spelled out so no player forgets it.

Two more rules:
- **The dead are out.** They don't speak, vote or act again.
- **No meta-talk.** Players stay in character: no "as an AI", and no mention of prompts or of the game's machinery.

---

## Personalities

Every player is dealt a **name**, an **archetype**, a **quirk** and a **village job**. The
personality shapes *how* a player talks. It never changes *what they are trying to win*: a gentle
Peacemaker who is secretly a wolf still wants the village dead.

**Archetypes.** Each player in a game gets a different one, until the list runs out.

| Archetype | Style |
|---|---|
| **Detective** | Methodical. Cites who said what and who voted how, and asks pointed questions. |
| **Paranoid** | Suspects everyone, accuses quickly, and reads hidden meaning into small things. |
| **Peacemaker** | Warm and diplomatic. Calms fights, looks for consensus, and is slow to accuse. |
| **Loudmouth** | Dramatic and theatrical, with strong opinions and plenty of exclamation marks. |
| **Quiet One** | Terse. Speaks in short sentences and observes more than they talk. |
| **Joker** | Sarcastic and teasing even when things are serious, but still makes real points. |
| **Follower** | Sides with whoever sounds most convincing and often echoes them. |
| **Contrarian** | When everyone agrees, pushes back and argues the other side. |
| **Village Elder** | Folksy and wise-sounding. Speaks in proverbs and tales from the village's past. |
| **Hothead** | Short-tempered. Takes accusations personally and fires back hard. |
| **Gossip** | Loves rumors. Repeats what others said and speculates about secret alliances. |

**Quirks.** Each player also has one habit:

- gets defensive when accused
- always ends with a question
- gives other players nicknames
- holds grudges against anyone who suspected them
- is overconfident in their own reads
- keeps bringing up food
- loves counting votes and citing numbers
- speaks very formally and politely
- changes their mind easily
- quotes made-up old village sayings
- distrusts anyone who stays quiet
- is loyal to the first person who defended them

**Jobs** are just for flavor: baker, blacksmith, farmer, innkeeper, fisher, herbalist, miller,
carpenter, shepherd, tailor, weaver, potter, hunter, candlemaker or brewer.

**Names** come from a pool of twenty, each with a different first letter (Alice, Bram, Clara,
Dmitri, Elena, …), so the chat is easy to follow.

---

## An example: Night 1 and Day 1

To keep the example short, it uses a smaller 7-player table (`--players 7`). Secretly, **Alice** and
**Dmitri** are the Werewolves, **Clara** is the Doctor, **Elena** is the Sheriff, and **Bram**,
**Finn** and **Greta** are Villagers.

**🌙 Night 1**

> **Wolves' chat.** Dmitri: *"Greta watches everyone too closely. Greta."* Alice: *"Bram talks too
> much and people listen to him. Bram."*
> The proposals differ, so both wolves make a final pick. Both choose **Bram**.
>
> **Doctor.** Clara protects Finn.
> **Sheriff.** Elena investigates Dmitri and learns: **Wolf**.

Bram was attacked and not protected, so Bram dies.

**☀️ Day 1**

> *"Bram was killed in the night. Bram was a Villager."*

**Round 1.** Most players speak carefully. Dmitri points at Finn: *"Awfully quiet, Finn. Why?"*
Dmitri asked him directly, so Finn answers at once, out of turn: *"I was listening. Someone has to."*
Greta, a Quiet One, rates her urge at 2 and stays silent.

**Round 2.** Elena makes her move: *"I'm the Sheriff. I checked Dmitri last night. He's a wolf."*
She named Dmitri, so he answers next: *"She's lying, and she knows it."* When Alice's turn comes,
she counterclaims: *"No, **I'm** the Sheriff, and I checked Elena. **She's** the wolf."* Now the
village has two Sheriff claims, and only one of them can be real. Four of the six players say
they are ready to vote, which is more than half, so the discussion ends.

**Vote.**

| Voter | Vote |
|---|---|
| Alice | Elena |
| Clara | Dmitri |
| Dmitri | Elena |
| Elena | Dmitri |
| Finn | Elena |
| Greta | Dmitri |

> *Dmitri 3, Elena 3 · tie.*

**Defenses.** Dmitri calls Elena a liar. Elena points out that Alice only "remembered" she was the
Sheriff after Elena spoke.

**Runoff.** The vote is now between Dmitri and Elena only. Dmitri and Elena each have one legal
choice, so their votes are cast automatically: Dmitri for Elena, and Elena for Dmitri. Finn,
persuaded by the defenses, switches to Dmitri.

> *Dmitri 4, Elena 2 · Dmitri is eliminated.*

**Last words.** Dmitri: *"You'll regret this, all of you."* His role is revealed: **Werewolf**.

One wolf (Alice) is left against four villagers, so the game goes on. Alice now has a problem: the
village knows Elena told the truth, which makes Alice's own Sheriff claim look very suspicious...

---

## Changing the setup

The number of players and wolves, whether there is a Doctor or a Sheriff, the discussion's caps
(rounds per day and speeches per player) and the day limit can all be changed from the command line. See the
[README](README.md#flags) for the flags.
