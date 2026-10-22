"""Small deterministic :mod:`pprint` port for native Python build tools.

The build-tool closure needs ``pformat`` for dictionaries containing ordinary
Python scalar/list/tuple/dict payloads and ``pprint`` for diagnostics.  This
port implements that generic data-model surface, including sorted dictionary
keys, depth limits, indentation, width, compact sequence packing, and recursive
container protection.  It deliberately falls back to an object's own ``repr``
for user-defined values instead of importing CPython's reflection-heavy
``pprint`` implementation.
"""

from __future__ import annotations


def _recursion(obj) -> str:
    return (
        "<Recursion on "
        + type(obj).__name__
        + " with id="
        + str(id(obj))
        + ">"
    )


def _ellipsis(obj) -> str:
    if isinstance(obj, dict):
        return "{...}"
    if isinstance(obj, list):
        return "[...]"
    if isinstance(obj, tuple):
        return "(...,)" if len(obj) == 1 else "(...)"
    return repr(obj)


def _dict_keys(obj, sort_dicts: bool):
    keys = list(obj.keys())
    if sort_dicts:
        # Build-tool state dictionaries use scalar, mutually comparable keys.
        # Let an unsupported mixed/custom ordering raise rather than inventing
        # a package-specific ordering rule.
        keys = sorted(keys)
    return keys


def _word_space_parts(line: str):
    """Return the ``non-space + following-space`` chunks pprint wraps."""
    parts = []
    i = 0
    while i < len(line):
        start = i
        while i < len(line) and not line[i].isspace():
            i += 1
        while i < len(line) and line[i].isspace():
            i += 1
        if i > start:
            parts.append(line[start:i])
    return parts


def _format_string(
    obj: str,
    indent_column: int,
    allowance: int,
    level: int,
    width: int,
) -> str:
    lines = obj.splitlines(True)
    if len(lines) == 0:
        return repr(obj)
    if level == 1:
        indent_column += 1
        allowance += 1
    max_width = width - indent_column
    chunks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        line_width = max_width
        if i == len(lines) - 1:
            line_width -= allowance
        line_rep = repr(line)
        if len(line_rep) <= line_width:
            chunks.append(line_rep)
        else:
            parts = _word_space_parts(line)
            current = ""
            j = 0
            while j < len(parts):
                part = parts[j]
                candidate = current + part
                part_width = max_width
                if j == len(parts) - 1 and i == len(lines) - 1:
                    part_width -= allowance
                if len(repr(candidate)) > part_width:
                    if current != "":
                        chunks.append(repr(current))
                    current = part
                else:
                    current = candidate
                j += 1
            if current != "":
                chunks.append(repr(current))
        i += 1
    if len(chunks) == 1:
        return chunks[0]
    separator = "\n" + " " * indent_column
    result = separator.join(chunks)
    if level == 1:
        return "(" + result + ")"
    return result


def _safe_repr(obj, depth, level: int, sort_dicts: bool, context) -> str:
    if isinstance(obj, dict):
        if len(obj) == 0:
            return "{}"
        objid = id(obj)
        if objid in context:
            return _recursion(obj)
        if depth is not None and level >= depth:
            return "{...}"
        context.append(objid)
        parts = []
        for key in _dict_keys(obj, sort_dicts):
            key_rep = _safe_repr(key, depth, level + 1, sort_dicts, context)
            value_rep = _safe_repr(
                obj[key], depth, level + 1, sort_dicts, context
            )
            parts.append(key_rep + ": " + value_rep)
        context.pop()
        return "{" + ", ".join(parts) + "}"

    if isinstance(obj, list) or isinstance(obj, tuple):
        if len(obj) == 0:
            return "[]" if isinstance(obj, list) else "()"
        objid = id(obj)
        if objid in context:
            return _recursion(obj)
        if depth is not None and level >= depth:
            return _ellipsis(obj)
        context.append(objid)
        parts = []
        for item in obj:
            parts.append(_safe_repr(item, depth, level + 1, sort_dicts, context))
        context.pop()
        joined = ", ".join(parts)
        if isinstance(obj, list):
            return "[" + joined + "]"
        if len(obj) == 1:
            return "(" + joined + ",)"
        return "(" + joined + ")"

    return repr(obj)


def _format_dict(
    obj,
    indent_column: int,
    allowance: int,
    level: int,
    indent_per_level: int,
    width: int,
    depth,
    compact: bool,
    sort_dicts: bool,
    context,
) -> str:
    objid = id(obj)
    if objid in context:
        return _recursion(obj)
    context.append(objid)
    out = "{"
    if indent_per_level > 1:
        out = out + " " * (indent_per_level - 1)
    keys = _dict_keys(obj, sort_dicts)
    child_indent = indent_column + indent_per_level
    i = 0
    while i < len(keys):
        if i > 0:
            out = out + ",\n" + " " * child_indent
        key = keys[i]
        key_rep = _safe_repr(key, depth, level, sort_dicts, context)
        out = out + key_rep + ": "
        value_allowance = allowance + 1 if i == len(keys) - 1 else 1
        out = out + _format(
            obj[key],
            child_indent + len(key_rep) + 2,
            value_allowance,
            level,
            indent_per_level,
            width,
            depth,
            compact,
            sort_dicts,
            context,
        )
        i += 1
    context.pop()
    return out + "}"


def _format_sequence(
    obj,
    indent_column: int,
    allowance: int,
    level: int,
    indent_per_level: int,
    width: int,
    depth,
    compact: bool,
    sort_dicts: bool,
    context,
) -> str:
    is_list = isinstance(obj, list)
    open_char = "[" if is_list else "("
    close_char = "]" if is_list else (",)" if len(obj) == 1 else ")")
    objid = id(obj)
    if objid in context:
        return _recursion(obj)
    context.append(objid)

    out = open_char
    child_indent = indent_column + indent_per_level
    if indent_per_level > 1:
        out = out + " " * (indent_per_level - 1)
    newline_delimiter = ",\n" + " " * child_indent
    delimiter = ""
    max_width = width - child_indent + 1
    remaining_width = max_width
    i = 0
    while i < len(obj):
        last = i == len(obj) - 1
        if last:
            max_width -= allowance + len(close_char)
            remaining_width -= allowance + len(close_char)
        item = obj[i]
        if compact:
            item_rep = _safe_repr(item, depth, level, sort_dicts, context)
            item_width = len(item_rep) + 2
            if remaining_width < item_width:
                remaining_width = max_width
                if delimiter != "":
                    delimiter = newline_delimiter
            if remaining_width >= item_width:
                remaining_width -= item_width
                out = out + delimiter + item_rep
                delimiter = ", "
                i += 1
                continue

        out = out + delimiter
        delimiter = newline_delimiter
        item_allowance = allowance + len(close_char) if last else 1
        out = out + _format(
            item,
            child_indent,
            item_allowance,
            level,
            indent_per_level,
            width,
            depth,
            compact,
            sort_dicts,
            context,
        )
        i += 1

    context.pop()
    return out + close_char


def _format(
    obj,
    indent_column: int,
    allowance: int,
    level: int,
    indent_per_level: int,
    width: int,
    depth,
    compact: bool,
    sort_dicts: bool,
    context,
) -> str:
    representation = _safe_repr(obj, depth, level, sort_dicts, context)
    if len(representation) <= width - indent_column - allowance:
        return representation
    if depth is not None and level >= depth:
        return _ellipsis(obj)
    if isinstance(obj, dict):
        return _format_dict(
            obj,
            indent_column,
            allowance,
            level + 1,
            indent_per_level,
            width,
            depth,
            compact,
            sort_dicts,
            context,
        )
    if isinstance(obj, list) or isinstance(obj, tuple):
        return _format_sequence(
            obj,
            indent_column,
            allowance,
            level + 1,
            indent_per_level,
            width,
            depth,
            compact,
            sort_dicts,
            context,
        )
    if isinstance(obj, str):
        return _format_string(
            obj,
            indent_column,
            allowance,
            level + 1,
            width,
        )
    return representation


def pformat(
    object,
    indent: int = 1,
    width: int = 80,
    depth=None,
    *,
    compact: bool = False,
    sort_dicts: bool = True,
    underscore_numbers: bool = False,
) -> str:
    indent = int(indent)
    width = int(width)
    if indent < 0:
        raise ValueError("indent must be >= 0")
    if depth is not None and depth <= 0:
        raise ValueError("depth must be > 0")
    if width == 0:
        raise ValueError("width must be != 0")
    if underscore_numbers:
        raise NotImplementedError(
            "pprint underscore_numbers awaits native integer format specs"
        )
    return _format(
        object,
        0,
        0,
        0,
        indent,
        width,
        depth,
        compact,
        sort_dicts,
        [],
    )


def pprint(
    object,
    stream=None,
    indent: int = 1,
    width: int = 80,
    depth=None,
    *,
    compact: bool = False,
    sort_dicts: bool = True,
    underscore_numbers: bool = False,
) -> None:
    text = pformat(
        object,
        indent,
        width,
        depth,
        compact=compact,
        sort_dicts=sort_dicts,
        underscore_numbers=underscore_numbers,
    )
    if stream is None:
        print(text)
    else:
        stream.write(text)
        stream.write("\n")


def saferepr(object) -> str:
    return _safe_repr(object, None, 0, True, [])
