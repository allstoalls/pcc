"""pcc.stdlib.logging — narrow ``logging`` skeleton."""
from __future__ import annotations


DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40
CRITICAL = 50


class Logger:
    def __init__(self, name: str, level: int = WARNING) -> None:
        self.name = name
        self.level = level

    def _emit(self, level: int, msg: str) -> None:
        if level < self.level:
            return
        # stderr write through the runtime print path — eventual
        # replacement: pcc.stdlib.sys.stderr.write once that exists.
        print(f"[{self.name}] {msg}")

    def debug(self, msg: str, *args, **kwargs) -> None:
        self._emit(DEBUG, msg)

    def info(self, msg: str, *args, **kwargs) -> None:
        self._emit(INFO, msg)

    def warning(self, msg: str, *args, **kwargs) -> None:
        self._emit(WARNING, msg)

    def error(self, msg: str, *args, **kwargs) -> None:
        self._emit(ERROR, msg)

    def critical(self, msg: str, *args, **kwargs) -> None:
        self._emit(CRITICAL, msg)

    def setLevel(self, level: int) -> None:
        self.level = level


_loggers: dict = {}


def getLogger(name: str = "root") -> Logger:
    if name not in _loggers:
        _loggers[name] = Logger(name)
    return _loggers[name]


def basicConfig(**kwargs) -> None:
    """No-op — pcc's self-host path doesn't maintain a handler registry."""
    return None
