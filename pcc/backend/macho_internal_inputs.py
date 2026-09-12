"""Ordered internal linker inputs shared by native and reference drivers."""

SCHEMA = "pcc.macho-internal-inputs.v1"


def read_internal_input_manifest(path: str) -> list[tuple[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except OSError as exc:
        raise ValueError("cannot read internal-input manifest: " + str(exc)) from exc
    if len(lines) < 2 or lines[0] != SCHEMA:
        raise ValueError("invalid internal-input manifest schema")
    try:
        expected = int(lines[1])
    except ValueError as exc:
        raise ValueError("invalid internal-input manifest count") from exc
    if expected < 1 or len(lines) != expected + 2:
        raise ValueError("internal-input manifest count mismatch")
    inputs = []
    for record in lines[2:]:
        kind, separator, raw_path = record.partition("\t")
        if separator != "\t" or kind not in ("ASM", "PCO") or not raw_path:
            raise ValueError("invalid internal-input manifest record")
        if "\t" in raw_path or "\n" in raw_path or "\r" in raw_path:
            raise ValueError("invalid internal-input manifest path")
        inputs.append((kind, raw_path))
    return inputs
