"""pcc-native subset of :mod:`warnings`.

The implementation owns the process-wide filter table, recording contexts and
warning emission.  It intentionally uses ``warn_explicit`` as the exact source
location boundary: compiled-frame discovery for ``warn(..., stacklevel=N)`` is
not available yet, so ``warn`` reports the stable ``<pcc>`` location instead of
inventing a caller frame.
"""

from __future__ import annotations

import re
import sys


class WarningMessage:
    def __init__(
        self,
        message,
        category,
        filename: str = "<pcc>",
        lineno: int = 0,
        file=None,
        line=None,
        source=None,
    ) -> None:
        self.message = message
        self.category = category
        self.filename = filename
        self.lineno = lineno
        self.file = file
        self.line = line
        self.source = source


# Entries have CPython's public shape:
#   (action, message-regex, category, module-regex, lineno)
filters = []
_active_recorders = []
_once_registry = []
_module_registry = []
_default_registry = []


def _copy_list(values):
    copied = []
    i = 0
    while i < len(values):
        copied.append(values[i])
        i += 1
    return copied


def _replace_list(target, values) -> None:
    target.clear()
    i = 0
    while i < len(values):
        target.append(values[i])
        i += 1


def _validate_action(action: str) -> str:
    if action == "all":
        return "always"
    if action not in ("error", "ignore", "always", "default", "module", "once"):
        raise AssertionError("invalid action: " + str(action))
    return action


def _validate_category(category) -> None:
    if not isinstance(category, type) or not issubclass(category, Warning):
        raise AssertionError("category must be a Warning subclass")


def filterwarnings(
    action: str,
    message: str = "",
    category=Warning,
    module: str = "",
    lineno: int = 0,
    append: bool = False,
) -> None:
    """Insert a CPython-shaped warning filter.

    Message expressions use case-insensitive ``re.match`` semantics; module
    expressions use case-sensitive ``re.match`` semantics.
    """
    action = _validate_action(action)
    if not isinstance(message, str):
        raise AssertionError("message must be a string")
    _validate_category(category)
    if not isinstance(module, str):
        raise AssertionError("module must be a string")
    if not isinstance(lineno, int) or lineno < 0:
        raise AssertionError("lineno must be an int >= 0")
    item = (action, message, category, module, lineno)
    if append:
        filters.append(item)
    else:
        filters.insert(0, item)


def simplefilter(
    action: str,
    category=Warning,
    lineno: int = 0,
    append: bool = False,
) -> None:
    filterwarnings(action, "", category, "", lineno, append)


def resetwarnings() -> None:
    filters.clear()


def _matches_filter(item, text: str, category, module: str, lineno: int) -> bool:
    action, message_expr, filter_category, module_expr, filter_lineno = item
    if message_expr != "" and re.match(message_expr, text, re.IGNORECASE) is None:
        return False
    if not issubclass(category, filter_category):
        return False
    if module_expr != "" and re.match(module_expr, module) is None:
        return False
    if filter_lineno != 0 and filter_lineno != lineno:
        return False
    return True


def _selected_action(text: str, category, module: str, lineno: int) -> str:
    i = 0
    while i < len(filters):
        item = filters[i]
        if _matches_filter(item, text, category, module, lineno):
            return item[0]
        i += 1
    return "default"


def _category_name(category) -> str:
    name = getattr(category, "__name__", None)
    if name is None:
        return "Warning"
    return str(name)


def formatwarning(
    message,
    category,
    filename: str,
    lineno: int,
    line=None,
) -> str:
    text = (
        str(filename)
        + ":"
        + str(lineno)
        + ": "
        + _category_name(category)
        + ": "
        + str(message)
        + "\n"
    )
    if line is not None:
        source_line = str(line).strip()
        if source_line != "":
            text += "  " + source_line + "\n"
    return text


def showwarning(
    message,
    category,
    filename: str,
    lineno: int,
    file=None,
    line=None,
) -> None:
    destination = file
    if destination is None:
        destination = sys.stderr
    destination.write(formatwarning(message, category, filename, lineno, line))


def _registry_key(text: str, category, suffix: str) -> str:
    return _category_name(category) + ":" + text + ":" + suffix


def _registry_seen(registry, key: str) -> bool:
    if registry is None:
        return False
    if isinstance(registry, dict):
        if registry.get(key, False):
            return True
        registry[key] = True
        return False
    i = 0
    while i < len(registry):
        if registry[i] == key:
            return True
        i += 1
    registry.append(key)
    return False


def warn_explicit(
    message,
    category,
    filename: str,
    lineno: int,
    module=None,
    registry=None,
    module_globals=None,
    source=None,
) -> None:
    _validate_category(category)
    if isinstance(message, Warning):
        warning = message
        category = type(message)
    else:
        warning = category(message)
    text = str(warning)
    module_name = module
    if module_name is None:
        module_name = str(filename)
    action = _selected_action(text, category, str(module_name), lineno)
    if action == "ignore":
        return None
    if action == "error":
        raise warning

    key = _registry_key(text, category, "")
    if action == "once":
        if _registry_seen(_once_registry, key):
            return None
    elif action == "module":
        if _registry_seen(_module_registry, key + str(module_name)):
            return None
    elif action == "default":
        location = str(filename) + ":" + str(lineno)
        selected_registry = registry
        if selected_registry is None:
            selected_registry = _default_registry
        if _registry_seen(selected_registry, key + location):
            return None

    if len(_active_recorders) > 0:
        _active_recorders[-1].append(
            WarningMessage(
                warning,
                category,
                filename,
                lineno,
                None,
                None,
                source,
            )
        )
        return None
    showwarning(warning, category, filename, lineno)
    return None


def warn(message, category=None, stacklevel: int = 1, source=None) -> None:
    """Issue a warning through the owned filter table.

    ``stacklevel`` is validated but source-frame attribution remains an
    explicit boundary; callers that need exact locations use
    :func:`warn_explicit`.
    """
    if category is None:
        category = UserWarning
    if not isinstance(stacklevel, int) or stacklevel < 1:
        raise ValueError("stacklevel must be >= 1")
    return warn_explicit(
        message,
        category,
        "<pcc>",
        0,
        module="__main__",
        source=source,
    )


class catch_warnings:
    """Save/restore filters and optionally record emitted warnings."""

    def __init__(self, *, record: bool = False, module=None) -> None:
        if module is not None:
            raise NotImplementedError(
                "catch_warnings(module=...) is not owned by pcc-native warnings"
            )
        self._record = record
        self._saved_filters = []
        self._log = []
        self._entered = False

    def __enter__(self):
        if self._entered:
            raise RuntimeError("Cannot enter catch_warnings twice")
        self._entered = True
        self._saved_filters = _copy_list(filters)
        if self._record:
            _active_recorders.append(self._log)
            return self._log
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self._entered:
            raise RuntimeError("Cannot exit catch_warnings without entering")
        if self._record and len(_active_recorders) > 0:
            _active_recorders.pop()
        _replace_list(filters, self._saved_filters)
        self._entered = False
        return False
