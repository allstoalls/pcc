"""Finite Windows path-string semantics for native build tools.

The Meson closure imports :data:`sep` even on POSIX hosts and also uses a
small set of lexical Windows-path operations while inspecting cross files.
Those operations do not require a Windows kernel and are implemented here as
pure string transformations.  Filesystem identity, junction resolution,
8.3-name expansion and Windows environment expansion are deliberately not
claimed by this module.

The owned surface is string-only.  CPython also accepts ``bytes`` and general
``os.PathLike`` values in several functions; accepting those without a common
representation contract would make the native boundary ambiguous, so they
fail closed.
"""
from __future__ import annotations


sep = "\\"
altsep = "/"
extsep = "."
pathsep = ";"
curdir = "."
pardir = ".."
defpath = ".;C:\\bin"
devnull = "nul"

_SEPARATORS = "\\/"


def _text(path):
    if not isinstance(path, str):
        raise NotImplementedError(
            "ntpath bytes and PathLike inputs are not runtime-owned"
        )
    return path


def normcase(path):
    return _text(path).replace(altsep, sep).lower()


def splitdrive(path):
    """Split a drive letter, UNC share or device prefix from *path*."""
    value = _text(path)
    normalized = value.replace(altsep, sep)
    length = len(normalized)

    if length >= 2 and normalized[1] == ":":
        return (value[:2], value[2:])

    if length >= 2 and normalized[0:2] == sep + sep:
        # ``\\?\UNC\server\share`` has a longer prefix before the server
        # component.  Other UNC and device paths start after the first pair.
        start = 2
        if length >= 8 and normalized[:8].lower() == "\\\\?\\unc\\".lower():
            start = 8
        first = normalized.find(sep, start)
        if first < 0:
            return (value, "")
        second = normalized.find(sep, first + 1)
        if second < 0:
            return (value, "")
        return (value[:second], value[second:])

    return (value[:0], value)


def splitroot(path):
    value = _text(path)
    drive, tail = splitdrive(value)
    if tail != "" and tail[0] in _SEPARATORS:
        return (drive, tail[:1], tail[1:])
    return (drive, tail[:0], tail)


def isabs(path):
    drive, root, _tail = splitroot(path)
    # Since Python 3.13, exactly one leading slash without a drive is not an
    # absolute Windows path.  UNC/device drives are absolute even when the
    # share itself has no trailing root separator.
    normalized_drive = drive.replace(altsep, sep)
    if normalized_drive.startswith(sep + sep):
        return True
    return drive != "" and root != ""


def join(path, *paths):
    result_drive, result_path = splitdrive(_text(path))
    for component in paths:
        component = _text(component)
        component_drive, component_path = splitdrive(component)
        if component_path != "" and component_path[0] in _SEPARATORS:
            if component_drive != "" or result_drive == "":
                result_drive = component_drive
            result_path = component_path
            continue
        if component_drive != "":
            if result_drive == "" or component_drive.lower() != result_drive.lower():
                result_drive = component_drive
                result_path = component_path
                continue
            # Preserve the spelling/case of the latest same drive.
            result_drive = component_drive
        if result_path != "" and result_path[-1] not in _SEPARATORS:
            result_path += sep
        result_path += component_path

    if (
        result_path != ""
        and result_path[0] not in _SEPARATORS
        and result_drive != ""
        and not result_drive.endswith(":")
    ):
        return result_drive + sep + result_path
    return result_drive + result_path


def split(path):
    drive, tail = splitdrive(_text(path))
    index = len(tail)
    while index > 0 and tail[index - 1] not in _SEPARATORS:
        index -= 1
    head = tail[:index]
    leaf = tail[index:]
    while len(head) > 1 and head[-1] in _SEPARATORS and head[-2] in _SEPARATORS:
        head = head[:-1]
    if head != "" and head.strip(_SEPARATORS) != "":
        head = head.rstrip(_SEPARATORS)
    return (drive + head, leaf)


def basename(path):
    return split(path)[1]


def dirname(path):
    return split(path)[0]


def splitext(path):
    value = _text(path)
    separator_index = value.rfind(sep)
    alternate_index = value.rfind(altsep)
    if alternate_index > separator_index:
        separator_index = alternate_index
    dot_index = value.rfind(extsep)
    if dot_index > separator_index:
        filename_index = separator_index + 1
        while filename_index < dot_index:
            if value[filename_index] != extsep:
                return (value[:dot_index], value[dot_index:])
            filename_index += 1
    return (value, value[:0])


def normpath(path):
    value = _text(path)
    if value == "":
        return curdir
    value = value.replace(altsep, sep)
    drive, tail = splitdrive(value)
    root = ""
    if tail.startswith(sep):
        root = sep
        tail = tail.lstrip(sep)

    components = []
    for component in tail.split(sep):
        if component == "" or component == curdir:
            continue
        if component == pardir:
            if len(components) > 0 and components[-1] != pardir:
                components.pop()
            elif root == "":
                components.append(component)
        else:
            components.append(component)

    normalized = root + sep.join(components)
    if normalized == "":
        normalized = curdir
    return drive + normalized


def commonprefix(paths):
    if len(paths) == 0:
        return ""
    values = [_text(path) for path in paths]
    shortest = values[0]
    longest = values[0]
    for value in values[1:]:
        if value < shortest:
            shortest = value
        if value > longest:
            longest = value
    index = 0
    limit = min(len(shortest), len(longest))
    while index < limit and shortest[index] == longest[index]:
        index += 1
    return shortest[:index]


def commonpath(paths):
    if len(paths) == 0:
        raise ValueError("commonpath() arg is an empty sequence")
    values = [_text(path) for path in paths]
    normalized = [normpath(value) for value in values]
    drives = []
    roots = []
    parts = []
    for value in normalized:
        drive, root, tail = splitroot(value)
        drives.append(drive)
        roots.append(root)
        parts.append([] if tail in ("", curdir) else tail.split(sep))
    first_drive = drives[0].lower()
    first_rooted = roots[0] != ""
    for index in range(1, len(drives)):
        if drives[index].lower() != first_drive:
            raise ValueError("Paths don't have the same drive")
        if (roots[index] != "") != first_rooted:
            raise ValueError("Can't mix absolute and relative paths")
    common = parts[0]
    for candidate in parts[1:]:
        index = 0
        limit = min(len(common), len(candidate))
        while index < limit and common[index].lower() == candidate[index].lower():
            index += 1
        common = common[:index]
    prefix = drives[0] + roots[0]
    if len(common) == 0:
        return prefix if prefix != "" else curdir
    if prefix != "" and prefix[-1] not in _SEPARATORS:
        prefix += sep
    return prefix + sep.join(common)


__all__ = [
    "sep",
    "altsep",
    "extsep",
    "pathsep",
    "curdir",
    "pardir",
    "defpath",
    "devnull",
    "normcase",
    "isabs",
    "join",
    "splitdrive",
    "splitroot",
    "split",
    "basename",
    "dirname",
    "splitext",
    "normpath",
    "commonprefix",
    "commonpath",
]
