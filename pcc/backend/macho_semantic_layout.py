"""Explicit semantic atom layout for pcc's owned Mach-O linker.

This pass is opt-in and consumes a manifest emitted by a semantics-owning
frontend.  The linker never guesses function boundaries, export reachability,
or hotness from symbol spelling.  A manifest is bound to the exact merged
``NativeObject`` bytes and describes complete, non-overlapping atoms in every
section it asks the linker to transform.

The finite v1 decision is:

* retain the transitive symbol-relocation closure from explicit roots and all
  non-eliminable atoms;
* drop only unreachable atoms marked ``eliminable``;
* lay retained atoms out hot, normal, cold with stable original-order ties;
* rewrite symbols, relocations and LC_DATA_IN_CODE offsets atomically; and
* require the dedicated precise-stackmap table to remain in canonical stable
  function-id order.

Any unmodelled byte, relocation, section target, symbol, or table shape rejects
the pass.  Callers then retain the ordinary from-scratch link; there is no
best-effort semantic rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import macho_spec as spec
from .macho_obj import DataInCodeRegion
from .native_object import (
    NativeObject,
    NativeObjectError,
    NativeRelocation,
    NativeSection,
    NativeSymbol,
    encode_native_object,
)
from .precise_stackmap import (
    ARCH_AARCH64,
    RUNTIME_TABLE_VERSION,
    PreciseStackMap,
    PreciseStackMapError,
    _payload_version,
    decode_stack_map,
    encode_stack_map,
    function_address_offsets,
    function_id,
)


SCHEMA = "pcc.macho-semantic-layout.v1"
FRONTEND_POLICY_SCHEMA = "pcc.frontend-macho-semantic-layout.v1"
TEMPERATURES = ("hot", "normal", "cold")
STACKMAP_POLICY = "stable-function-id-v1"


class SemanticLayoutError(ValueError):
    """The supplied semantic proof is incomplete or inconsistent."""


def native_object_sha256(obj: NativeObject) -> str:
    return hashlib.sha256(encode_native_object(obj)).hexdigest()


def _clean_text(value: object, field: str) -> str:
    text = str(value)
    if (
        not text
        or not text.isascii()
        or "\x00" in text
        or "\n" in text
        or "\r" in text
    ):
        raise SemanticLayoutError("invalid " + field)
    return text


def _clean_digest(value: object, field: str) -> str:
    text = _clean_text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise SemanticLayoutError("invalid " + field + " digest")
    return text


def _canonical_names(values: Iterable[object], field: str) -> tuple[str, ...]:
    names = tuple(sorted({_clean_text(value, field) for value in values}))
    return names


@dataclass(frozen=True)
class SemanticAtom:
    name: str
    segname: str
    sectname: str
    offset: int
    size: int
    align_log2: int
    temperature: str
    eliminable: bool

    @classmethod
    def create(
        cls,
        *,
        name: str,
        segname: str,
        sectname: str,
        offset: int,
        size: int,
        align_log2: int,
        temperature: str = "normal",
        eliminable: bool = False,
    ) -> "SemanticAtom":
        clean_temperature = _clean_text(temperature, "atom temperature")
        if clean_temperature not in TEMPERATURES:
            raise SemanticLayoutError(
                "unknown semantic atom temperature " + clean_temperature
            )
        clean_offset = int(offset)
        clean_size = int(size)
        clean_alignment = int(align_log2)
        if clean_offset < 0 or clean_size <= 0:
            raise SemanticLayoutError("semantic atom range must be positive")
        if not 0 <= clean_alignment <= 31:
            raise SemanticLayoutError("semantic atom alignment is invalid")
        if clean_offset & ((1 << clean_alignment) - 1):
            raise SemanticLayoutError("semantic atom input offset is misaligned")
        return cls(
            name=_clean_text(name, "atom name"),
            segname=_clean_text(segname, "atom segment"),
            sectname=_clean_text(sectname, "atom section"),
            offset=clean_offset,
            size=clean_size,
            align_log2=clean_alignment,
            temperature=clean_temperature,
            eliminable=bool(eliminable),
        )

    @property
    def section_key(self) -> tuple[str, str]:
        return self.segname, self.sectname

    @property
    def end(self) -> int:
        return self.offset + self.size

    def payload(self) -> dict[str, object]:
        return {
            "align_log2": self.align_log2,
            "eliminable": self.eliminable,
            "name": self.name,
            "offset": self.offset,
            "section": [self.segname, self.sectname],
            "size": self.size,
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class SemanticLayoutManifest:
    object_sha256: str
    entry: str
    roots: tuple[str, ...]
    atoms: tuple[SemanticAtom, ...]
    stackmap_policy: str = STACKMAP_POLICY

    @classmethod
    def create(
        cls,
        *,
        object_sha256: str,
        entry: str,
        roots: Iterable[str],
        atoms: Iterable[SemanticAtom],
        stackmap_policy: str = STACKMAP_POLICY,
    ) -> "SemanticLayoutManifest":
        ordered_atoms = tuple(
            sorted(
                atoms,
                key=lambda atom: (
                    atom.segname,
                    atom.sectname,
                    atom.offset,
                    atom.name,
                ),
            )
        )
        if not ordered_atoms:
            raise SemanticLayoutError("semantic layout has no atoms")
        if len({atom.name for atom in ordered_atoms}) != len(ordered_atoms):
            raise SemanticLayoutError("semantic layout duplicates an atom name")
        policy = _clean_text(stackmap_policy, "stackmap policy")
        if policy != STACKMAP_POLICY:
            raise SemanticLayoutError("unsupported semantic stackmap policy")
        return cls(
            object_sha256=_clean_digest(object_sha256, "native object"),
            entry=_clean_text(entry, "entry symbol"),
            roots=_canonical_names(roots, "root symbol"),
            atoms=ordered_atoms,
            stackmap_policy=policy,
        )

    def payload(self) -> dict[str, object]:
        return {
            "atoms": [atom.payload() for atom in self.atoms],
            "entry": self.entry,
            "object_sha256": self.object_sha256,
            "roots": list(self.roots),
            "schema": SCHEMA,
            "stackmap_policy": self.stackmap_policy,
        }

    def digest(self) -> str:
        raw = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SemanticLayoutPlan:
    manifest_digest: str
    kept_atoms: tuple[str, ...]
    dropped_atoms: tuple[str, ...]
    output_order: tuple[str, ...]
    input_bytes: int
    output_bytes: int
    packed_runtime_tables: tuple[str, ...]


@dataclass(frozen=True)
class SemanticLayoutResult:
    native_object: NativeObject
    plan: SemanticLayoutPlan


@dataclass(frozen=True)
class FrontendSemanticFunction:
    symbol: str
    temperature: str
    eliminable: bool

    @classmethod
    def create(
        cls,
        *,
        symbol: object,
        temperature: object,
        eliminable: object,
    ) -> "FrontendSemanticFunction":
        clean_temperature = _clean_text(temperature, "function temperature")
        if clean_temperature not in TEMPERATURES:
            raise SemanticLayoutError(
                "unknown frontend function temperature " + clean_temperature
            )
        if not isinstance(eliminable, bool):
            raise SemanticLayoutError(
                "frontend function eliminable flag must be boolean"
            )
        return cls(
            symbol=_clean_text(symbol, "function symbol"),
            temperature=clean_temperature,
            eliminable=eliminable,
        )

    def payload(self) -> dict[str, object]:
        return {
            "eliminable": self.eliminable,
            "symbol": self.symbol,
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class FrontendSemanticLayoutPolicy:
    entry: str
    roots: tuple[str, ...]
    functions: tuple[FrontendSemanticFunction, ...]

    @classmethod
    def create(
        cls,
        *,
        entry: object,
        roots: Iterable[object],
        functions: Iterable[FrontendSemanticFunction],
    ) -> "FrontendSemanticLayoutPolicy":
        ordered = tuple(sorted(functions, key=lambda item: item.symbol))
        if not ordered:
            raise SemanticLayoutError("frontend semantic policy has no functions")
        if len({item.symbol for item in ordered}) != len(ordered):
            raise SemanticLayoutError(
                "frontend semantic policy duplicates a function symbol"
            )
        clean_entry = _clean_text(entry, "frontend semantic entry")
        symbols = {item.symbol for item in ordered}
        if clean_entry not in symbols:
            raise SemanticLayoutError(
                "frontend semantic entry is not a declared function"
            )
        clean_roots = _canonical_names(roots, "frontend semantic root")
        missing_roots = [root for root in clean_roots if root not in symbols]
        if missing_roots:
            raise SemanticLayoutError(
                "frontend semantic roots are not declared functions: "
                + repr(missing_roots)
            )
        return cls(clean_entry, clean_roots, ordered)

    def payload(self) -> dict[str, object]:
        return {
            "entry": self.entry,
            "functions": [item.payload() for item in self.functions],
            "roots": list(self.roots),
            "schema": FRONTEND_POLICY_SCHEMA,
        }

    def digest(self) -> str:
        raw = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return hashlib.sha256(raw).hexdigest()


def manifest_from_payload(payload: Mapping[str, Any]) -> SemanticLayoutManifest:
    expected = {
        "atoms",
        "entry",
        "object_sha256",
        "roots",
        "schema",
        "stackmap_policy",
    }
    if set(payload) != expected:
        raise SemanticLayoutError("semantic layout fields do not match schema")
    if payload.get("schema") != SCHEMA:
        raise SemanticLayoutError("unsupported semantic layout schema")
    raw_atoms = payload.get("atoms")
    if not isinstance(raw_atoms, list):
        raise SemanticLayoutError("semantic layout atoms must be a list")
    atoms: list[SemanticAtom] = []
    for raw in raw_atoms:
        if not isinstance(raw, Mapping) or set(raw) != {
            "align_log2",
            "eliminable",
            "name",
            "offset",
            "section",
            "size",
            "temperature",
        }:
            raise SemanticLayoutError("semantic atom fields do not match schema")
        section = raw.get("section")
        if not isinstance(section, list) or len(section) != 2:
            raise SemanticLayoutError("semantic atom section must have two names")
        atoms.append(
            SemanticAtom.create(
                name=raw["name"],
                segname=section[0],
                sectname=section[1],
                offset=raw["offset"],
                size=raw["size"],
                align_log2=raw["align_log2"],
                temperature=raw["temperature"],
                eliminable=raw["eliminable"],
            )
        )
    roots = payload.get("roots")
    if not isinstance(roots, list):
        raise SemanticLayoutError("semantic roots must be a list")
    return SemanticLayoutManifest.create(
        object_sha256=payload.get("object_sha256"),
        entry=payload.get("entry"),
        roots=roots,
        atoms=atoms,
        stackmap_policy=payload.get("stackmap_policy"),
    )


def load_manifest(path: str | Path) -> SemanticLayoutManifest:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticLayoutError(f"cannot read semantic layout manifest: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SemanticLayoutError("semantic layout manifest must be an object")
    return manifest_from_payload(payload)


def frontend_policy_from_payload(
    payload: Mapping[str, Any],
) -> FrontendSemanticLayoutPolicy:
    if set(payload) != {"entry", "functions", "roots", "schema"}:
        raise SemanticLayoutError(
            "frontend semantic policy fields do not match schema"
        )
    if payload.get("schema") != FRONTEND_POLICY_SCHEMA:
        raise SemanticLayoutError("unsupported frontend semantic policy schema")
    raw_functions = payload.get("functions")
    if not isinstance(raw_functions, list):
        raise SemanticLayoutError(
            "frontend semantic policy functions must be a list"
        )
    functions: list[FrontendSemanticFunction] = []
    for raw_function in raw_functions:
        if not isinstance(raw_function, Mapping) or set(raw_function) != {
            "eliminable",
            "symbol",
            "temperature",
        }:
            raise SemanticLayoutError(
                "frontend semantic function fields do not match schema"
            )
        functions.append(
            FrontendSemanticFunction.create(
                symbol=raw_function.get("symbol"),
                temperature=raw_function.get("temperature"),
                eliminable=raw_function.get("eliminable"),
            )
        )
    roots = payload.get("roots")
    if not isinstance(roots, list):
        raise SemanticLayoutError("frontend semantic roots must be a list")
    return FrontendSemanticLayoutPolicy.create(
        entry=payload.get("entry"),
        roots=roots,
        functions=functions,
    )


def load_frontend_policy(path: str | Path) -> FrontendSemanticLayoutPolicy:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticLayoutError(
            f"cannot read frontend semantic policy: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SemanticLayoutError(
            "frontend semantic policy must be an object"
        )
    return frontend_policy_from_payload(payload)


def _offset_alignment(offset: int, section_align_log2: int) -> int:
    if offset == 0:
        return section_align_log2
    alignment = 0
    value = offset
    while alignment < section_align_log2 and value & 1 == 0:
        alignment += 1
        value >>= 1
    return alignment


def materialize_frontend_manifest(
    obj: NativeObject,
    policy: FrontendSemanticLayoutPolicy,
) -> SemanticLayoutManifest:
    """Bind frontend symbol semantics to exact merged-object atom ranges.

    Symbol offsets are mechanical evidence, not semantic guesses.  Unknown
    runtime/archive symbols become normal, non-eliminable atoms.  A frontend
    symbol missing from the merged text section rejects the whole opt-in pass.
    """

    if not isinstance(obj, NativeObject):
        raise SemanticLayoutError(
            "frontend semantic layout input must be a NativeObject"
        )
    text_indices = [
        index
        for index, section in enumerate(obj.sections, start=1)
        if (section.segname, section.sectname) == ("__TEXT", "__text")
    ]
    if len(text_indices) != 1:
        raise SemanticLayoutError(
            "frontend semantic layout requires one __TEXT,__text section"
        )
    section_index = text_indices[0]
    section = obj.sections[section_index - 1]
    if not section.data or section.zerofill_size:
        raise SemanticLayoutError(
            "frontend semantic layout requires content-backed text"
        )
    policies = {item.symbol: item for item in policy.functions}
    defined_text = [
        symbol for symbol in obj.symbols if symbol.section_index == section_index
    ]
    symbols_by_name = {symbol.name: symbol for symbol in defined_text}
    missing = sorted(set(policies) - set(symbols_by_name))
    if missing:
        raise SemanticLayoutError(
            "frontend semantic functions are absent from merged text: "
            + repr(missing)
        )
    if policy.entry not in symbols_by_name:
        raise SemanticLayoutError(
            "frontend semantic entry is absent from merged text"
        )
    missing_roots = [root for root in policy.roots if root not in symbols_by_name]
    if missing_roots:
        raise SemanticLayoutError(
            "frontend semantic roots are absent from merged text: "
            + repr(missing_roots)
        )
    by_offset: dict[int, list[NativeSymbol]] = {}
    for symbol in defined_text:
        if symbol.offset < 0 or symbol.offset >= len(section.data):
            raise SemanticLayoutError(
                f"text symbol {symbol.name!r} does not anchor nonempty bytes"
            )
        by_offset.setdefault(symbol.offset, []).append(symbol)
    if not by_offset:
        raise SemanticLayoutError("merged text has no symbol anchors")
    offsets = sorted(by_offset)
    atoms: list[SemanticAtom] = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(section.data)
        if end <= offset:
            raise SemanticLayoutError("text symbol offsets are not increasing")
        symbols = sorted(by_offset[offset], key=lambda item: item.name)
        declared = [policies[item.name] for item in symbols if item.name in policies]
        anchor = (
            sorted(item.symbol for item in declared)[0]
            if declared
            else symbols[0].name
        )
        has_unknown_alias = len(declared) != len(symbols)
        eliminable = bool(declared) and not has_unknown_alias and all(
            item.eliminable for item in declared
        )
        if any(item.temperature == "hot" for item in declared):
            temperature = "hot"
        elif (
            declared
            and not has_unknown_alias
            and all(item.temperature == "cold" for item in declared)
        ):
            temperature = "cold"
        else:
            temperature = "normal"
        atoms.append(
            SemanticAtom.create(
                name=anchor,
                segname="__TEXT",
                sectname="__text",
                offset=offset,
                size=end - offset,
                align_log2=_offset_alignment(offset, section.align_log2),
                temperature=temperature,
                eliminable=eliminable,
            )
        )
    return SemanticLayoutManifest.create(
        object_sha256=native_object_sha256(obj),
        entry=policy.entry,
        roots=policy.roots,
        atoms=atoms,
    )


def _align_up(value: int, align_log2: int) -> int:
    mask = (1 << align_log2) - 1
    return (value + mask) & ~mask


def _atom_containing(
    atoms: tuple[SemanticAtom, ...],
    offset: int,
    size: int = 1,
) -> SemanticAtom | None:
    end = offset + size
    owners = [atom for atom in atoms if atom.offset <= offset and end <= atom.end]
    if len(owners) > 1:
        raise SemanticLayoutError("semantic atoms overlap")
    return owners[0] if owners else None


def _stackmap_bindings(
    obj: NativeObject,
) -> dict[
    int,
    tuple[
        PreciseStackMap,
        tuple[str, ...],
        tuple[NativeRelocation, ...],
    ],
]:
    bindings: dict[
        int,
        tuple[
            PreciseStackMap,
            tuple[str, ...],
            tuple[NativeRelocation, ...],
        ],
    ] = {}
    symbol_names = [symbol.name for symbol in obj.symbols]
    for section_index, section in enumerate(obj.sections, start=1):
        if (section.segname, section.sectname) != ("__DATA", "__pcc_stackmaps"):
            continue
        if _payload_version(section.data) == RUNTIME_TABLE_VERSION:
            raise SemanticLayoutError(
                "semantic layout cannot rebuild a version 3 runtime stack-map table"
            )
        try:
            decoded = decode_stack_map(
                section.data,
                expected_arch=ARCH_AARCH64,
                final_image=False,
            )
            if encode_stack_map(decoded, final_image=False) != section.data:
                raise SemanticLayoutError(
                    "precise stackmap table is not canonically packed"
                )
        except PreciseStackMapError as exc:
            raise SemanticLayoutError(f"invalid precise stackmap table: {exc}") from exc
        if section.data_in_code or section.zerofill_size:
            raise SemanticLayoutError(
                "precise stackmap table must be content-backed metadata"
            )
        offsets = function_address_offsets(section.data)
        if len(section.relocations) != len(offsets):
            raise SemanticLayoutError(
                "precise stackmap table needs one relocation per function"
            )
        relocation_by_offset: dict[int, NativeRelocation] = {}
        for relocation in section.relocations:
            if relocation.offset in relocation_by_offset:
                raise SemanticLayoutError(
                    "precise stackmap table duplicates a function relocation"
                )
            if (
                relocation.symbol_index is None
                or relocation.type != spec.ARM64_RELOC_UNSIGNED
                or relocation.pcrel
                or relocation.length != 3
                or relocation.addend
                or relocation.target_section_index is not None
                or relocation.minuend_index is not None
                or relocation.target_offset is not None
            ):
                raise SemanticLayoutError(
                    "precise stackmap function relocation is not exact"
                )
            relocation_by_offset[relocation.offset] = relocation
        if set(relocation_by_offset) != set(offsets):
            raise SemanticLayoutError(
                "precise stackmap relocations do not match address fields"
            )
        targets: list[str] = []
        templates: list[NativeRelocation] = []
        for index, offset in enumerate(offsets):
            function = decoded.functions[index]
            if function.function_address != 0:
                raise SemanticLayoutError(
                    "relocatable stackmap function address must be zero"
                )
            relocation = relocation_by_offset[offset]
            assert relocation.symbol_index is not None
            target_symbol = obj.symbols[relocation.symbol_index]
            if target_symbol.section_index == 0:
                raise SemanticLayoutError(
                    "precise stackmap function target must be defined"
                )
            target_name = symbol_names[relocation.symbol_index]
            if function.function_id != function_id(target_name):
                raise SemanticLayoutError(
                    "precise stackmap function id does not match its symbol"
                )
            targets.append(target_name)
            templates.append(relocation)
        bindings[section_index] = (
            decoded,
            tuple(targets),
            tuple(templates),
        )
    return bindings


def _validate_stackmap_tables(obj: NativeObject) -> tuple[str, ...]:
    bindings = _stackmap_bindings(obj)
    return tuple("__DATA,__pcc_stackmaps" for _index in sorted(bindings))


def apply_semantic_layout(
    obj: NativeObject,
    manifest: SemanticLayoutManifest,
) -> SemanticLayoutResult:
    """Apply one exact semantic plan or raise before returning any artifact."""

    if not isinstance(obj, NativeObject):
        raise SemanticLayoutError("semantic layout input must be a NativeObject")
    if native_object_sha256(obj) != manifest.object_sha256:
        raise SemanticLayoutError("semantic layout object digest mismatch")

    section_index_by_key = {
        (section.segname, section.sectname): index
        for index, section in enumerate(obj.sections, start=1)
    }
    atoms_by_section: dict[int, tuple[SemanticAtom, ...]] = {}
    for key in sorted({atom.section_key for atom in manifest.atoms}):
        section_index = section_index_by_key.get(key)
        if section_index is None:
            raise SemanticLayoutError(f"semantic atom names missing section {key!r}")
        section = obj.sections[section_index - 1]
        if section.zerofill_size or not section.data:
            raise SemanticLayoutError("semantic atoms require a content section")
        if key == ("__DATA", "__pcc_stackmaps"):
            raise SemanticLayoutError("stackmap packing is not an atom transform")
        atoms = tuple(
            sorted(
                (atom for atom in manifest.atoms if atom.section_key == key),
                key=lambda atom: (atom.offset, atom.name),
            )
        )
        previous_end = 0
        for atom in atoms:
            if atom.offset < previous_end or atom.end > len(section.data):
                raise SemanticLayoutError("semantic atom range overlaps or escapes section")
            if any(section.data[previous_end:atom.offset]):
                raise SemanticLayoutError(
                    "unowned nonzero bytes occur between semantic atoms"
                )
            previous_end = atom.end
        if any(section.data[previous_end:]):
            raise SemanticLayoutError(
                "unowned nonzero bytes occur after semantic atoms"
            )
        atoms_by_section[section_index] = atoms

    symbol_to_atom: dict[str, SemanticAtom] = {}
    defined_names = {symbol.name for symbol in obj.symbols if symbol.section_index}
    if manifest.entry not in defined_names:
        raise SemanticLayoutError("semantic entry symbol is not defined")
    for root in manifest.roots:
        if root not in defined_names:
            raise SemanticLayoutError(f"semantic root {root!r} is not defined")
    for atom in manifest.atoms:
        anchors = [
            symbol
            for symbol in obj.symbols
            if symbol.name == atom.name
            and symbol.section_index == section_index_by_key[atom.section_key]
            and symbol.offset == atom.offset
        ]
        if len(anchors) != 1:
            raise SemanticLayoutError(
                f"semantic atom {atom.name!r} lacks its exact anchor symbol"
            )
    for symbol in obj.symbols:
        atoms = atoms_by_section.get(symbol.section_index)
        if atoms is None:
            continue
        owner = _atom_containing(atoms, symbol.offset)
        if owner is None:
            raise SemanticLayoutError(
                f"symbol {symbol.name!r} is outside every semantic atom"
            )
        symbol_to_atom[symbol.name] = owner

    edges: dict[str, set[str]] = {atom.name: set() for atom in manifest.atoms}
    roots = {manifest.entry, *manifest.roots}
    roots.update(atom.name for atom in manifest.atoms if not atom.eliminable)
    symbol_names = [symbol.name for symbol in obj.symbols]
    stackmap_bindings = _stackmap_bindings(obj)

    for source_section_index, section in enumerate(obj.sections, start=1):
        source_atoms = atoms_by_section.get(source_section_index)
        # A precise stackmap function address is metadata owned by that exact
        # function, not a semantic reachability root.  The table is filtered
        # and its address relocations are rebuilt after the kept set freezes.
        if source_section_index in stackmap_bindings:
            continue
        for relocation in section.relocations:
            width = 1 << relocation.length
            source_atom = (
                _atom_containing(source_atoms, relocation.offset, width)
                if source_atoms is not None
                else None
            )
            if source_atoms is not None and source_atom is None:
                raise SemanticLayoutError(
                    "relocation occurs outside every semantic atom"
                )
            if relocation.target_section_index is not None:
                if (
                    source_atoms is not None
                    or relocation.target_section_index in atoms_by_section
                ):
                    raise SemanticLayoutError(
                        "section-target relocation crosses a semantic atom boundary"
                    )
                continue
            assert relocation.symbol_index is not None
            targets = [symbol_names[relocation.symbol_index]]
            if relocation.minuend_index is not None:
                targets.append(symbol_names[relocation.minuend_index])
            for target_name in targets:
                target_atom = symbol_to_atom.get(target_name)
                if target_atom is None:
                    continue
                if source_atom is None:
                    roots.add(target_atom.name)
                else:
                    edges[source_atom.name].add(target_atom.name)
        if source_atoms is not None:
            for region in section.data_in_code:
                if _atom_containing(source_atoms, region.offset, region.length) is None:
                    raise SemanticLayoutError(
                        "data-in-code range crosses a semantic atom boundary"
                    )

    reachable: set[str] = set()
    pending = sorted(
        {symbol_to_atom[name].name for name in roots if name in symbol_to_atom},
        reverse=True,
    )
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for target in sorted(edges[current], reverse=True):
            if target not in reachable:
                pending.append(target)

    dropped = tuple(
        atom.name
        for atom in manifest.atoms
        if atom.name not in reachable and atom.eliminable
    )
    illegal_unreachable = [
        atom.name
        for atom in manifest.atoms
        if atom.name not in reachable and not atom.eliminable
    ]
    if illegal_unreachable:
        raise SemanticLayoutError("non-eliminable semantic atom became unreachable")
    kept = {atom.name for atom in manifest.atoms if atom.name not in dropped}

    moved_offset: dict[str, int] = {}
    output_order: list[str] = []
    transformed_sections: list[NativeSection] = []
    temperature_rank = {name: index for index, name in enumerate(TEMPERATURES)}
    for section_index, section in enumerate(obj.sections, start=1):
        atoms = atoms_by_section.get(section_index)
        stackmap_binding = stackmap_bindings.get(section_index)
        if stackmap_binding is not None:
            decoded, target_names, templates = stackmap_binding
            packed_functions = []
            packed_templates: list[NativeRelocation] = []
            for function, target_name, template in zip(
                decoded.functions, target_names, templates
            ):
                target_atom = symbol_to_atom.get(target_name)
                if target_atom is None:
                    raise SemanticLayoutError(
                        "precise stackmap target is outside semantic text atoms"
                    )
                if target_atom.name not in kept:
                    continue
                packed_functions.append(function)
                packed_templates.append(template)
            packed_value = PreciseStackMap(
                arch=decoded.arch,
                functions=tuple(packed_functions),
            )
            packed_data = encode_stack_map(packed_value, final_image=False)
            packed_offsets = function_address_offsets(packed_data)
            packed_relocations = tuple(
                NativeRelocation(
                    offset=packed_offsets[index],
                    symbol_index=template.symbol_index,
                    type=template.type,
                    pcrel=template.pcrel,
                    length=template.length,
                    addend=template.addend,
                    target_section_index=template.target_section_index,
                    minuend_index=template.minuend_index,
                    target_offset=template.target_offset,
                )
                for index, template in enumerate(packed_templates)
            )
            transformed_sections.append(
                NativeSection(
                    segname=section.segname,
                    sectname=section.sectname,
                    flags=section.flags,
                    align_log2=section.align_log2,
                    data=packed_data,
                    relocations=packed_relocations,
                    zerofill_size=0,
                    data_in_code=(),
                )
            )
            continue
        if atoms is None:
            transformed_sections.append(section)
            continue
        ordered = sorted(
            (atom for atom in atoms if atom.name in kept),
            key=lambda atom: (temperature_rank[atom.temperature], atom.offset, atom.name),
        )
        data = bytearray()
        for atom in ordered:
            new_offset = _align_up(len(data), atom.align_log2)
            data.extend(b"\0" * (new_offset - len(data)))
            moved_offset[atom.name] = new_offset
            output_order.append(atom.name)
            data.extend(section.data[atom.offset:atom.end])
        transformed_sections.append(
            NativeSection(
                segname=section.segname,
                sectname=section.sectname,
                flags=section.flags,
                align_log2=section.align_log2,
                data=bytes(data),
                relocations=(),
                zerofill_size=0,
                data_in_code=(),
            )
        )

    kept_symbols: list[NativeSymbol] = []
    for symbol in obj.symbols:
        owner = symbol_to_atom.get(symbol.name)
        if owner is not None and owner.name not in kept:
            continue
        if owner is None:
            kept_symbols.append(symbol)
            continue
        kept_symbols.append(
            NativeSymbol(
                name=symbol.name,
                section_index=symbol.section_index,
                offset=moved_offset[owner.name] + (symbol.offset - owner.offset),
                external=symbol.external,
                private_external=symbol.private_external,
            )
        )

    retained_names = {symbol.name for symbol in kept_symbols}
    reloc_specs: list[list[tuple[NativeRelocation, int, str | None, str | None]]] = [
        [] for _section in obj.sections
    ]
    retained_undefined: set[str] = set()
    for section_index, section in enumerate(obj.sections, start=1):
        atoms = atoms_by_section.get(section_index)
        relocation_source = (
            transformed_sections[section_index - 1].relocations
            if section_index in stackmap_bindings
            else section.relocations
        )
        for relocation in relocation_source:
            width = 1 << relocation.length
            source_atom = (
                _atom_containing(atoms, relocation.offset, width)
                if atoms is not None
                else None
            )
            if source_atom is not None and source_atom.name not in kept:
                continue
            new_offset = relocation.offset
            if source_atom is not None:
                new_offset = moved_offset[source_atom.name] + (
                    relocation.offset - source_atom.offset
                )
            target_name = (
                symbol_names[relocation.symbol_index]
                if relocation.symbol_index is not None
                else None
            )
            minuend_name = (
                symbol_names[relocation.minuend_index]
                if relocation.minuend_index is not None
                else None
            )
            for name in (target_name, minuend_name):
                if name is None:
                    continue
                if name not in retained_names:
                    raise SemanticLayoutError(
                        f"retained relocation targets dropped symbol {name!r}"
                    )
                old_symbol = obj.symbols[symbol_names.index(name)]
                if old_symbol.section_index == 0:
                    retained_undefined.add(name)
            reloc_specs[section_index - 1].append(
                (relocation, new_offset, target_name, minuend_name)
            )

    # Undefined entries without a retained relocation are not link inputs any
    # more.  This prevents dead atoms from keeping an otherwise unused dylib
    # import alive.
    kept_symbols = [
        symbol
        for symbol in kept_symbols
        if symbol.section_index != 0 or symbol.name in retained_undefined
    ]
    locals_defined = sorted(
        (symbol for symbol in kept_symbols if symbol.section_index and not symbol.external),
        key=lambda symbol: (symbol.section_index, symbol.offset, symbol.name),
    )
    extern_defined = sorted(
        (symbol for symbol in kept_symbols if symbol.section_index and symbol.external),
        key=lambda symbol: (symbol.section_index, symbol.offset, symbol.name),
    )
    undefined = sorted(
        (symbol for symbol in kept_symbols if symbol.section_index == 0),
        key=lambda symbol: symbol.name,
    )
    canonical_symbols = tuple(locals_defined + extern_defined + undefined)
    new_symbol_index = {
        symbol.name: index for index, symbol in enumerate(canonical_symbols)
    }

    final_sections: list[NativeSection] = []
    for section_index, section in enumerate(obj.sections, start=1):
        base = transformed_sections[section_index - 1]
        atoms = atoms_by_section.get(section_index)
        relocations: list[NativeRelocation] = []
        for old, new_offset, target_name, minuend_name in reloc_specs[section_index - 1]:
            relocations.append(
                NativeRelocation(
                    offset=new_offset,
                    symbol_index=(
                        new_symbol_index[target_name]
                        if target_name is not None else None
                    ),
                    type=old.type,
                    pcrel=old.pcrel,
                    length=old.length,
                    addend=old.addend,
                    target_section_index=old.target_section_index,
                    minuend_index=(
                        new_symbol_index[minuend_name]
                        if minuend_name is not None else None
                    ),
                    target_offset=old.target_offset,
                )
            )
        regions: list[DataInCodeRegion] = []
        for region in section.data_in_code:
            if atoms is None:
                regions.append(region)
                continue
            owner = _atom_containing(atoms, region.offset, region.length)
            assert owner is not None
            if owner.name not in kept:
                continue
            regions.append(
                DataInCodeRegion(
                    offset=moved_offset[owner.name] + (region.offset - owner.offset),
                    length=region.length,
                    kind=region.kind,
                )
            )
        final_sections.append(
            NativeSection(
                segname=base.segname,
                sectname=base.sectname,
                flags=base.flags,
                align_log2=base.align_log2,
                data=base.data,
                relocations=tuple(sorted(relocations, key=lambda item: item.offset)),
                zerofill_size=base.zerofill_size,
                data_in_code=tuple(sorted(regions, key=lambda item: item.offset)),
            )
        )

    try:
        result_object = NativeObject(tuple(final_sections), canonical_symbols)
    except NativeObjectError as exc:
        raise SemanticLayoutError(
            f"semantic layout produced an invalid native object: {exc}"
        ) from exc
    packed_tables = _validate_stackmap_tables(result_object)
    plan = SemanticLayoutPlan(
        manifest_digest=manifest.digest(),
        kept_atoms=tuple(atom.name for atom in manifest.atoms if atom.name in kept),
        dropped_atoms=dropped,
        output_order=tuple(output_order),
        input_bytes=sum(len(section.data) for section in obj.sections),
        output_bytes=sum(len(section.data) for section in result_object.sections),
        packed_runtime_tables=packed_tables,
    )
    return SemanticLayoutResult(result_object, plan)


__all__ = [
    "FRONTEND_POLICY_SCHEMA",
    "SCHEMA",
    "STACKMAP_POLICY",
    "FrontendSemanticFunction",
    "FrontendSemanticLayoutPolicy",
    "SemanticAtom",
    "SemanticLayoutError",
    "SemanticLayoutManifest",
    "SemanticLayoutPlan",
    "SemanticLayoutResult",
    "apply_semantic_layout",
    "frontend_policy_from_payload",
    "load_frontend_policy",
    "load_manifest",
    "materialize_frontend_manifest",
    "manifest_from_payload",
    "native_object_sha256",
]
