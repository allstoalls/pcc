"""Exact local-name replacement shared by owned IR transformations."""

_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$-"


def _quoted_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        quote = text.find('"', index)
        escape = text.find("\\", index)
        if quote < 0:
            return len(text)
        if escape >= 0 and escape < quote:
            index = escape + 2
        else:
            return quote + 1
    return len(text)


def replace_local_names(text: str, replacements: dict[str, str]) -> str:
    """Replace complete %names, preserving quoted text and comments.

    Values in replacements are complete operand spellings. Like the compiled
    default tier, names are maximal tokens; chunked copying avoids producing
    one Python string/list entry for every unchanged character.
    """
    if not replacements:
        return text
    pieces: list[str] = []
    literal = 0
    index = 0
    quote = text.find('"')
    comment = text.find(";")
    while index < len(text):
        found = text.find("%", index)
        if found < 0:
            break
        if quote >= 0 and quote < index:
            quote = text.find('"', index)
        if comment >= 0 and comment < index:
            comment = text.find(";", index)
        if quote >= 0 and quote < found and (comment < 0 or quote < comment):
            index = _quoted_end(text, quote)
            continue
        if comment >= 0 and comment < found:
            end = text.find("\n", comment)
            if end < 0:
                break
            index = end + 1
            continue
        end = found + 1
        while end < len(text) and text[end] in _NAME_CHARS:
            end += 1
        name = text[found + 1:end]
        replacement = replacements.get(name)
        if name and replacement is not None:
            pieces.append(text[literal:found])
            pieces.append(replacement)
            literal = end
        index = end
    if not pieces:
        return text
    pieces.append(text[literal:])
    return "".join(pieces)
