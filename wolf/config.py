from dataclasses import asdict, dataclass

from .personalities import NAMES


@dataclass
class GameConfig:
    players: int = 9
    wolves: int = 2
    doctors: int = 1
    sheriffs: int = 1
    max_rounds: int = 4  # discussion rounds per day, at most
    max_speeches: int = 4  # times each player may speak per day; replies count too
    max_replies_in_row: int = 3  # out-of-turn replies in a row before the round carries on
    day_turns_per_player: int = 5  # a day's discussion has at most this × living players turns
    speech_words: int = 70
    wolf_chat_words: int = 50
    defense_words: int = 60
    last_words_words: int = 50
    reason_words: int = 20
    max_days: int = 10
    retries: int = 2  # extra attempts after an invalid reply, before a random fallback
    seed: int | None = None

    @property
    def villagers(self) -> int:
        return self.players - self.wolves - self.doctors - self.sheriffs

    def role_summary(self) -> str:
        """e.g. '2 Werewolves, 1 Doctor, 1 Sheriff, 3 Villagers'"""
        parts = []
        for count, one, many in (
            (self.wolves, "Werewolf", "Werewolves"),
            (self.doctors, "Doctor", "Doctors"),
            (self.sheriffs, "Sheriff", "Sheriffs"),
            (self.villagers, "Villager", "Villagers"),
        ):
            if count:
                parts.append(f"{count} {one if count == 1 else many}")
        return ", ".join(parts)

    def validate(self) -> None:
        if self.players > len(NAMES):
            raise ValueError(f"at most {len(NAMES)} players are supported")
        if self.wolves < 1:
            raise ValueError("the game needs at least 1 werewolf")
        if min(self.doctors, self.sheriffs) < 0 or self.villagers < 0:
            raise ValueError(f"{self.players} players can't hold {self.role_summary()}")
        if self.wolves >= self.players - self.wolves:
            raise ValueError("too many wolves: they would win before the game starts")
        if min(self.max_rounds, self.max_speeches, self.day_turns_per_player, self.max_days) < 1:
            raise ValueError("max_rounds, max_speeches, day_turns_per_player and max_days must be at least 1")
        if self.max_replies_in_row < 0:
            raise ValueError("max_replies_in_row can't be negative")

    def to_dict(self) -> dict:
        return asdict(self)
