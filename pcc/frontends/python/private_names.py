"""Lexical Python private names shared by class layouts and exports."""


def mangle_private_name(class_name: str, name: str) -> str:
    if not name.startswith("__") or name.endswith("__") or "." in name:
        return name
    owner = class_name.lstrip("_")
    if not owner:
        return name
    return f"_{owner}{name}"
