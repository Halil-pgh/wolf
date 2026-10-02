from typing import Protocol


class BackendError(Exception):
    """The backend could not produce a reply (process failed, timeout, rate limit, bad output).

    fatal=True means retrying can't help (not logged in, usage limit reached), so the game stops at once.
    """

    def __init__(self, message: str = "", *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class LLMBackend(Protocol):
    # How many complete() calls may run at the same time. 1 means the engine runs everything in sequence.
    max_concurrency: int

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        """Return the model's reply as a dict matching `schema`, or raise BackendError."""
        ...

    def stats(self) -> dict:
        """Usage counters for the end-of-game summary, e.g. {"calls": 97, "seconds": 812.4}."""
        ...
