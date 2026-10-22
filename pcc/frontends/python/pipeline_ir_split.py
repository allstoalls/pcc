"""LLVM text sharding for pass workers and self-backend object workers."""

from __future__ import annotations

from pcc.frontends.python.pipeline_ir_text import (
    defined_function_name_from_line,
    export_split_function_declaration,
    export_split_function_text,
    function_declaration_from_define_line,
    global_declaration_from_definition_line,
    line_has_internal_linkage,
    llvm_global_name_char,
    private_symbol_rename_map,
    rename_llvm_global_refs,
    rename_symbol_name,
)


def ir_global_definition_line(line: str) -> bool:
    if not line.startswith("@"):
        return False
    if " = " not in line:
        return False
    if " external " in (" " + line + " "):
        return False
    padded = " " + line + " "
    return " global " in padded or " constant " in padded


def export_split_global_line(line: str) -> str:
    line = line.replace(" = internal ", " = ", 1)
    return line.replace(" = private ", " = ", 1)


def self_backend_local_frame_map_line(line: str) -> bool:
    """Return whether *line* is an immutable precise-root descriptor.

    Self-backend object shards are emitted independently, and precise
    stack-map planning needs the descriptor initializer before the objects are
    linked. These internal constants stay local to shards that reference
    them; unfamiliar descriptor/reference shapes retain the copy-all path.
    Mutable globals must continue to use the single-definition path.
    """
    if not line_has_internal_linkage(line):
        return False
    padded = " " + line + " "
    if " constant i32 " not in padded:
        return False
    return (
        ".pcc.gc.frame.map." in line
        or ".pcc.vthread.frame.map." in line
    )


def _literal_local_frame_map_name(line: str) -> str:
    """Recognize only dependency-free descriptors emitted by our frontend."""
    words = line.split()
    if (
        len(words) != 6
        or not words[0].startswith("@")
        or words[1] != "="
        or words[2] not in ("internal", "private")
        or words[3] != "constant"
        or words[4] != "i32"
    ):
        return ""
    name = words[0][1:]
    if not (
        name.startswith(".pcc.gc.frame.map.")
        or name.startswith(".pcc.vthread.frame.map.")
    ):
        return ""
    for char in name:
        if ord(char) >= 128 or not llvm_global_name_char(char):
            return ""
    count = words[5]
    digits = count[1:] if count.startswith("-") else count
    if not digits or len(digits) > 10:
        return ""
    for char in digits:
        if char < "0" or char > "9":
            return ""
    if int(count) < -2147483648 or int(count) > 2147483647:
        return ""
    return name


def _local_frame_map_references(
    text: str, names: set[str],
) -> set[str] | None:
    """Conservatively collect map uses; None keeps every descriptor.

    Scan reserved map operands, including aliases, metadata and initializer
    expressions. String/comment false positives retain extra maps safely.
    Quoted/escaped names and assembly need a richer lexer, so do not prune.
    """
    if '@"' in text or "\\" in text:
        return None
    position = text.find("asm")
    while position >= 0:
        end = position + 3
        if (
            (position == 0 or not llvm_global_name_char(text[position - 1]))
            and (end == len(text) or not llvm_global_name_char(text[end]))
        ):
            return None
        position = text.find("asm", end)
    references: set[str] = set()
    for prefix in ("@.pcc.gc.frame.map.", "@.pcc.vthread.frame.map."):
        position = text.find(prefix)
        while position >= 0:
            end = position + len(prefix)
            while end < len(text) and llvm_global_name_char(text[end]):
                end += 1
            name = text[position + 1:end]
            if name in names:
                references.add(name)
                if len(references) == len(names):
                    return references
            position = text.find(prefix, end)
    return references


def split_python_ir_module_for_pass_shards(
    ir_text: str,
    *,
    export_prefix: str,
    shard_bytes: int,
) -> list[str]:
    lines = str(ir_text).splitlines()
    shared_lines: list[str] = []
    global_defs_raw: list[str] = []
    functions_raw: list[tuple[str, str, str, bool]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("define "):
            body_lines = [line]
            index += 1
            while index < len(lines):
                body_lines.append(lines[index])
                if lines[index].startswith("}"):
                    index += 1
                    break
                index += 1
            body = "\n".join(body_lines)
            name = defined_function_name_from_line(line)
            functions_raw.append(
                (
                    name,
                    body,
                    function_declaration_from_define_line(line),
                    line_has_internal_linkage(line),
                )
            )
            continue
        if ir_global_definition_line(line):
            global_defs_raw.append(line)
        else:
            shared_lines.append(line)
        index += 1

    if len(functions_raw) <= 1:
        return [ir_text]

    rename_map = private_symbol_rename_map(
        global_defs_raw,
        functions_raw,
        export_prefix,
    )
    shared = rename_llvm_global_refs(
        "\n".join(shared_lines), rename_map
    ).strip()
    global_defs = [
        export_split_global_line(rename_llvm_global_refs(line, rename_map))
        for line in global_defs_raw
    ]
    global_declarations = []
    for line in global_defs_raw:
        declaration = global_declaration_from_definition_line(
            rename_llvm_global_refs(line, rename_map)
        )
        if declaration:
            global_declarations.append(declaration)
    functions = []
    for name, body, declaration, _is_internal in functions_raw:
        functions.append(
            (
                rename_symbol_name(name, rename_map),
                export_split_function_text(
                    rename_llvm_global_refs(body, rename_map)
                ),
                export_split_function_declaration(
                    rename_llvm_global_refs(declaration, rename_map)
                ),
            )
        )
    all_function_declarations: list[tuple[str, str]] = []
    for name, _body, declaration in functions:
        if declaration:
            all_function_declarations.append((name, declaration))

    def make_shard(
        body_parts: list[tuple[str, str, str]],
        *,
        include_global_defs: bool,
    ) -> str:
        body_names = set()
        for name, _body, _declaration in body_parts:
            body_names.add(name)
        pieces: list[str] = []
        if shared:
            pieces.append(shared)
        if include_global_defs:
            pieces.extend(global_defs)
        else:
            pieces.extend(global_declarations)
        for name, declaration in all_function_declarations:
            if name not in body_names:
                pieces.append(declaration)
        for _name, body, _declaration in body_parts:
            pieces.append(body)
        non_empty_pieces: list[str] = []
        for piece in pieces:
            if piece:
                non_empty_pieces.append(piece)
        return "\n\n".join(non_empty_pieces).strip() + "\n"

    shards: list[str] = []
    if global_defs:
        shards.append(make_shard([], include_global_defs=True))
    current: list[tuple[str, str, str]] = []
    current_bytes = 0
    for function in functions:
        function_bytes = len(function[1])
        if current and current_bytes + function_bytes > shard_bytes:
            shards.append(make_shard(current, include_global_defs=False))
            current = []
            current_bytes = 0
        current.append(function)
        current_bytes += function_bytes
    if current:
        shards.append(make_shard(current, include_global_defs=False))
    if len(shards) <= 1:
        return [ir_text]
    return shards


def split_self_backend_ir_module_for_object_shards(
    ir_text: str,
    *,
    export_prefix: str,
    shard_bytes: int,
) -> list[str]:
    lines = str(ir_text).splitlines()
    shared_lines: list[str] = []
    global_lines_raw: list[str] = []
    functions_raw: list[tuple[str, str, str, bool]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("define "):
            body_lines = [line]
            index += 1
            while index < len(lines):
                body_lines.append(lines[index])
                if lines[index].startswith("}"):
                    index += 1
                    break
                index += 1
            body = "\n".join(body_lines)
            functions_raw.append(
                (
                    defined_function_name_from_line(line),
                    body,
                    "",
                    line_has_internal_linkage(line),
                )
            )
            continue
        if ir_global_definition_line(line):
            global_lines_raw.append(line)
        else:
            shared_lines.append(line)
        index += 1

    if len(functions_raw) <= 1:
        return [ir_text]
    local_frame_map_lines = []
    exported_global_lines_raw = []
    for line in global_lines_raw:
        if self_backend_local_frame_map_line(line):
            local_frame_map_lines.append(line)
        else:
            exported_global_lines_raw.append(line)
    rename_map = private_symbol_rename_map(
        exported_global_lines_raw,
        functions_raw,
        export_prefix,
    )
    shared = rename_llvm_global_refs(
        "\n".join(shared_lines), rename_map
    ).strip()
    global_lines = []
    for line in exported_global_lines_raw:
        global_lines.append(
            export_split_global_line(rename_llvm_global_refs(line, rename_map))
        )
    functions = []
    total_function_bytes = 0
    for _name, body, _declaration, _is_internal in functions_raw:
        function_text = export_split_function_text(
            rename_llvm_global_refs(body, rename_map)
        )
        functions.append(function_text)
        total_function_bytes += len(function_text)
    # The original single-output path returns the input unchanged. Avoid any
    # descriptor/reference analysis when no object sharding can take place.
    if not global_lines and total_function_bytes <= shard_bytes:
        return [ir_text]

    local_frame_map_names: list[str] = []
    local_frame_map_name_set: set[str] = set()
    # Small descriptor sets offer limited duplication savings; keep their
    # copy-all path without adding scans to large function bodies.
    can_prune_local_maps = len(local_frame_map_lines) > 3
    if can_prune_local_maps:
        for line in local_frame_map_lines:
            name = _literal_local_frame_map_name(line)
            if not name or name in local_frame_map_name_set:
                can_prune_local_maps = False
                break
            local_frame_map_names.append(name)
            local_frame_map_name_set.add(name)
    shared_map_references = None
    if can_prune_local_maps:
        shared_map_references = _local_frame_map_references(
            shared, local_frame_map_name_set,
        )
        if (
            shared_map_references is not None
            and len(shared_map_references) == len(local_frame_map_name_set)
        ):
            shared_map_references = None

    def make_shard(body_parts: list[str]) -> str:
        pieces = []
        if shared:
            pieces.append(shared)
        if body_parts:
            needed: set[str] = set()
            prune = shared_map_references is not None
            if shared_map_references is not None:
                for name in shared_map_references:
                    needed.add(name)
                for part in body_parts:
                    references = _local_frame_map_references(
                        part, local_frame_map_name_set,
                    )
                    if references is None:
                        prune = False
                        break
                    for name in references:
                        needed.add(name)
            for index, line in enumerate(local_frame_map_lines):
                if not prune or local_frame_map_names[index] in needed:
                    pieces.append(line)
        for part in body_parts:
            if part:
                pieces.append(part)
        return "\n\n".join(pieces).strip() + "\n"

    shards: list[str] = []
    if global_lines:
        shards.append(make_shard(global_lines))
    current: list[str] = []
    current_bytes = 0
    for function_text in functions:
        function_bytes = len(function_text)
        if current and current_bytes + function_bytes > shard_bytes:
            shards.append(make_shard(current))
            current = []
            current_bytes = 0
        current.append(function_text)
        current_bytes += function_bytes
    if current:
        shards.append(make_shard(current))
    if len(shards) <= 1:
        return [ir_text]
    return shards
