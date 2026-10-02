from enum import Enum


class Team(str, Enum):
    VILLAGE = "village"
    WOLVES = "wolves"


class Role(str, Enum):
    WEREWOLF = "Werewolf"
    DOCTOR = "Doctor"
    SHERIFF = "Sheriff"
    VILLAGER = "Villager"

    @property
    def team(self) -> Team:
        return Team.WOLVES if self is Role.WEREWOLF else Team.VILLAGE

    @property
    def emoji(self) -> str:
        return ROLE_EMOJI[self]

    @property
    def color(self) -> str:
        return ROLE_COLOR[self]


ROLE_EMOJI = {
    Role.WEREWOLF: "🐺",
    Role.DOCTOR: "🩺",
    Role.SHERIFF: "⭐",
    Role.VILLAGER: "🌾",
}

ROLE_COLOR = {
    Role.WEREWOLF: "red",
    Role.DOCTOR: "green",
    Role.SHERIFF: "yellow",
    Role.VILLAGER: "white",
}
