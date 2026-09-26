class FactoryError(Exception):
    """Expected, user-facing failure. The CLI prints the message without a traceback."""

    exit_code = 1


class ConfigError(FactoryError):
    exit_code = 2


class Refused(FactoryError):
    """A safety rule blocked the action. Never worked around."""

    exit_code = 3


class Stop(FactoryError):
    """The run cannot continue; carries the outcome recorded in metrics."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
