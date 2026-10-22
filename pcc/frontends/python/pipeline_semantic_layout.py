"""Pure frontend policy for the opt-in owned Mach-O semantic layout pass.

The Python frontend owns semantic facts; it does not guess machine offsets.
This module records exact final symbol names, linkage-derived eliminability,
and explicit LLVM hot/cold attributes.  The owned linker later combines this
policy with the merged ``NativeObject`` and binds the resulting atom manifest
to that object's digest.

Keep this module free of :mod:`pcc.backend` imports.  Compiled pcc stages call
the linker through a host subprocess and must not pull linker implementation
modules into their own closure.
"""

from __future__ import annotations

import json


SCHEMA = "pcc.frontend-macho-semantic-layout.v1"
MODE_ENV = "PCC_MACHO_SEMANTIC_LAYOUT"
ROOTS_ENV = "PCC_MACHO_SEMANTIC_ROOTS"

_FALSE_VALUES = ("", "0", "false", "no", "off", "disabled")
_TRUE_VALUES = ("1", "true", "yes", "on", "enabled")


class FrontendSemanticLayoutError(ValueError):
    """Frontend semantic policy is malformed or requested out of scope."""


def semantic_layout_enabled(
    raw_mode: object,
    *,
    platform: str,
    link_mode: str,
) -> bool:
    value = str(raw_mode or "").strip().lower()
    if value in _FALSE_VALUES:
        return False
    if value not in _TRUE_VALUES:
        raise FrontendSemanticLayoutError(
            "invalid " + MODE_ENV + " value " + repr(value)
        )
    if platform != "darwin" or link_mode != "pcc":
        raise FrontendSemanticLayoutError(
            MODE_ENV + " requires the pcc-owned Darwin Mach-O linker"
        )
    return True


def _simple_symbol(name: str) -> str:
    text = str(name or "")
    if not text or not (text[0].isalpha() or text[0] in ("_", ".", "$")):
        raise FrontendSemanticLayoutError(
            "semantic layout requires a simple LLVM symbol, got " + repr(text)
        )
    for char in text[1:]:
        if not (char.isalnum() or char in ("_", ".", "$")):
            raise FrontendSemanticLayoutError(
                "semantic layout requires a simple LLVM symbol, got "
                + repr(text)
            )
    return text


def _stable_symbol_digest(text: str) -> str:
    """Mirror self_backend_module_symbols' deterministic 40-bit namespace."""
    modulus = 1099511627776
    value = 0
    index = 0
    while index < len(text):
        value = (value * 131 + ord(text[index])) % modulus
        index += 1
    digits = "0123456789abcdef"
    out = ""
    shift = 36
    while shift >= 0:
        out += digits[(value >> shift) & 15]
        shift -= 4
    return out


def _attribute_groups(ir_text: str) -> dict[str, tuple[str, ...]]:
    groups: dict[str, tuple[str, ...]] = {}
    for raw_line in ir_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("attributes #"):
            continue
        marker_end = line.find(" =")
        open_brace = line.find("{")
        close_brace = line.rfind("}")
        if marker_end < 12 or open_brace < 0 or close_brace <= open_brace:
            raise FrontendSemanticLayoutError(
                "malformed LLVM attribute group in semantic layout input"
            )
        group = line[len("attributes #") : marker_end].strip()
        if not group.isdigit() or group in groups:
            raise FrontendSemanticLayoutError(
                "invalid or duplicate LLVM attribute group " + repr(group)
            )
        groups[group] = tuple(line[open_brace + 1 : close_brace].split())
    return groups


def _header_attribute_group(line: str) -> str:
    brace = line.rfind("{")
    head = line if brace < 0 else line[:brace]
    for token in reversed(head.split()):
        if token.startswith("#") and token[1:].isdigit():
            return token[1:]
    return ""


def _temperature(line: str, groups: dict[str, tuple[str, ...]]) -> str:
    tokens = tuple(line.replace("(", " ").replace(")", " ").split())
    group = _header_attribute_group(line)
    attrs = groups.get(group, ()) if group else ()
    is_hot = "hot" in tokens or "hot" in attrs
    is_cold = "cold" in tokens or "cold" in attrs
    if is_hot and is_cold:
        raise FrontendSemanticLayoutError(
            "LLVM function cannot be both hot and cold"
        )
    if is_hot:
        return "hot"
    if is_cold:
        return "cold"
    return "normal"


def _module_functions(
    ir_text: str,
    *,
    defined_function_name_from_line,
    global_name_from_definition_line,
    ir_global_definition_line,
    line_has_internal_linkage,
) -> list[tuple[str, bool, str]]:
    groups = _attribute_groups(ir_text)
    definitions: list[tuple[str, bool, str]] = []
    for raw_line in ir_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("define "):
            continue
        name = _simple_symbol(defined_function_name_from_line(line))
        definitions.append(
            (
                name,
                bool(line_has_internal_linkage(line)),
                _temperature(line, groups),
            )
        )
    if not definitions:
        return []
    names = [item[0] for item in definitions]
    if len(set(names)) != len(names):
        raise FrontendSemanticLayoutError(
            "semantic layout input defines a function more than once"
        )
    global_definitions: list[tuple[str, bool]] = []
    for raw_line in ir_text.splitlines():
        line = raw_line.strip()
        if not ir_global_definition_line(line):
            continue
        name = _simple_symbol(global_name_from_definition_line(line))
        global_definitions.append((name, bool(line_has_internal_linkage(line))))
    global_names = [item[0] for item in global_definitions]
    if len(set(global_names)) != len(global_names):
        raise FrontendSemanticLayoutError(
            "semantic layout input defines a global more than once"
        )
    public = sorted(set(
        [name for name, internal, _temp in definitions if not internal]
        + [name for name, internal in global_definitions if not internal]
    ))
    all_defined = sorted(set(names + global_names))
    seed = "\n".join(public if public else all_defined)
    internal_prefix = "__pccmod_" + _stable_symbol_digest(seed) + "_"
    result: list[tuple[str, bool, str]] = []
    for name, internal, temperature in definitions:
        native_name = "_" + (internal_prefix + name if internal else name)
        result.append((native_name, internal, temperature))
    return result


def build_frontend_semantic_layout_policy(
    ir_texts: list[str],
    *,
    entry: str = "main",
    root_names: tuple[str, ...] = (),
    defined_function_name_from_line,
    global_name_from_definition_line,
    ir_global_definition_line,
    line_has_internal_linkage,
) -> dict[str, object]:
    functions: list[tuple[str, bool, str]] = []
    logical_to_native: dict[str, str] = {}
    for ir_text in ir_texts:
        module_functions = _module_functions(
            str(ir_text),
            defined_function_name_from_line=defined_function_name_from_line,
            global_name_from_definition_line=global_name_from_definition_line,
            ir_global_definition_line=ir_global_definition_line,
            line_has_internal_linkage=line_has_internal_linkage,
        )
        for native_name, internal, temperature in module_functions:
            if any(existing[0] == native_name for existing in functions):
                raise FrontendSemanticLayoutError(
                    "semantic layout produces duplicate native symbol "
                    + repr(native_name)
                )
            functions.append((native_name, internal, temperature))
            logical = native_name[1:]
            if not internal:
                logical_to_native[logical] = native_name
    if not functions:
        raise FrontendSemanticLayoutError(
            "semantic layout input contains no function definitions"
        )
    entry_name = _simple_symbol(entry)
    entry_symbol = logical_to_native.get(entry_name)
    if entry_symbol is None:
        raise FrontendSemanticLayoutError(
            "semantic layout entry is not a public function: " + repr(entry_name)
        )
    roots: list[str] = []
    for raw_name in root_names:
        logical_name = _simple_symbol(raw_name)
        native_name = logical_to_native.get(logical_name)
        if native_name is None:
            raise FrontendSemanticLayoutError(
                "semantic layout root is not a public function: "
                + repr(logical_name)
            )
        if native_name not in roots:
            roots.append(native_name)
    functions.sort(key=lambda item: item[0])
    return {
        "entry": entry_symbol,
        "functions": [
            {
                "eliminable": internal,
                "symbol": native_name,
                "temperature": temperature,
            }
            for native_name, internal, temperature in functions
        ],
        "roots": sorted(roots),
        "schema": SCHEMA,
    }


def parse_root_names(raw_roots: object) -> tuple[str, ...]:
    text = str(raw_roots or "").strip()
    if not text:
        return ()
    roots: list[str] = []
    for piece in text.split(","):
        name = _simple_symbol(piece.strip())
        if name not in roots:
            roots.append(name)
    return tuple(roots)


def write_frontend_semantic_layout_policy(
    path: str,
    ir_texts: list[str],
    *,
    root_names: tuple[str, ...],
    defined_function_name_from_line,
    global_name_from_definition_line,
    ir_global_definition_line,
    line_has_internal_linkage,
) -> None:
    payload = build_frontend_semantic_layout_policy(
        ir_texts,
        root_names=root_names,
        defined_function_name_from_line=defined_function_name_from_line,
        global_name_from_definition_line=global_name_from_definition_line,
        ir_global_definition_line=ir_global_definition_line,
        line_has_internal_linkage=line_has_internal_linkage,
    )
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        stream.write("\n")


__all__ = [
    "FrontendSemanticLayoutError",
    "MODE_ENV",
    "ROOTS_ENV",
    "SCHEMA",
    "build_frontend_semantic_layout_policy",
    "parse_root_names",
    "semantic_layout_enabled",
    "write_frontend_semantic_layout_policy",
]
