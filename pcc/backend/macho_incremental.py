"""Content-addressed incremental state for pcc's owned Mach-O linker.

LINK-P2-INCREMENTAL has two safe reuse levels:

* an exact action-key hit reuses the already validated executable image;
* a same-layout edit reuses the resolved/merged ``NativeObject`` (including
  archive selection), patches every current pcc-native input payload into its
  recorded prefix layout, and runs only final executable layout/relocation/
  signing.

The second level is deliberately narrow.  Native inputs must precede external
Mach-O inputs, their section/symbol/relocation metadata must be identical, and
section-target relocations are excluded because relocatable linking normalizes
their stored fields.  Anything outside that proof boundary is a normal cold
prepare, never an approximate incremental link.  Every published image still
passes the caller's structural validator, and cache corruption is a miss.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .macho_exec import (
    link_prepared_executable, prepare_executable_object, validate_executable_imports,
)
from .macho_link import LinkInput
from .native_object import (
    NativeObject,
    NativeObjectError,
    NativeObjectView,
    PackedNativeObject,
    decode_native_object,
    decode_packed_native_object,
    encode_native_object,
)
from .precise_stackmap import PreciseStackMapError, precise_stackmap_runtime_format
from .self_backend_cache_identity import macho_linker_source_identity


INCREMENTAL_CACHE_ENV = "PCC_MACHO_INCREMENTAL_LINK_CACHE"
INCREMENTAL_CACHE_DIR_ENV = "PCC_MACHO_INCREMENTAL_LINK_CACHE_DIR"

_OBJECT_CACHE_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE"
_OBJECT_CACHE_DIR_ENV = "PCC_SELF_BACKEND_OBJECT_CACHE_DIR"

_CACHE_SCHEMA = b"pcc.macho-incremental-link.v2\0"
_ASSEMBLY_KEY_SCHEMA = b"pcc.macho-incremental-assembly.v1\0"
_IMAGE_KEY_SCHEMA = b"pcc.macho-incremental-image.v1\0"
_LAYOUT_KEY_SCHEMA = b"pcc.macho-incremental-layout.v1\0"
_MAX_CACHE_PAYLOAD = 2 * 1024 * 1024 * 1024

_FALSE_VALUES = frozenset({
    "0", "false", "no", "off", "disable", "disabled",
})
_TRUE_VALUES = frozenset({
    "1", "true", "yes", "on", "enable", "enabled",
})


class IncrementalLinkError(RuntimeError):
    """Incremental state is malformed or violates its reuse contract."""


@dataclass
class IncrementalLinkStats:
    assembly_hits: int = 0
    assembly_misses: int = 0
    image_hits: int = 0
    image_misses: int = 0
    merged_hits: int = 0
    merged_misses: int = 0
    incremental_fallbacks: int = 0
    uncacheable_links: int = 0


def _feed(digest, label: bytes, payload: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _feed_text(digest, label: bytes, value: str) -> None:
    _feed(digest, label, value.encode("utf-8"))


def _feed_integer(digest, label: bytes, value: int | None) -> None:
    raw = b"none" if value is None else str(value).encode("ascii")
    _feed(digest, label, raw)


def _native_input(value: object) -> NativeObject | None:
    if isinstance(value, NativeObject):
        return value
    if isinstance(value, NativeObjectView):
        return value.native
    return None


def _hash_native_shape(digest, native: NativeObject) -> None:
    """Bind all merge-relevant metadata while intentionally omitting bytes."""

    _feed_integer(digest, b"section-count", len(native.sections))
    _feed_integer(digest, b"symbol-count", len(native.symbols))
    for symbol in native.symbols:
        _feed_text(digest, b"symbol-name", symbol.name)
        _feed_integer(digest, b"symbol-section", symbol.section_index)
        _feed_integer(digest, b"symbol-offset", symbol.offset)
        _feed(digest, b"symbol-external", b"1" if symbol.external else b"0")
        _feed(
            digest,
            b"symbol-private-external",
            b"1" if symbol.private_external else b"0",
        )
        _feed(digest, b"symbol-weak-reference", b"1" if symbol.weak_reference else b"0")
    for section in native.sections:
        _feed_text(digest, b"segment-name", section.segname)
        _feed_text(digest, b"section-name", section.sectname)
        _feed_integer(digest, b"section-flags", section.flags)
        _feed_integer(digest, b"section-align", section.align_log2)
        _feed_integer(digest, b"section-data-size", len(section.data))
        _feed_integer(digest, b"section-zerofill", section.zerofill_size)
        _feed_integer(digest, b"relocation-count", len(section.relocations))
        for relocation in section.relocations:
            _feed_integer(digest, b"relocation-offset", relocation.offset)
            _feed_integer(
                digest, b"relocation-symbol", relocation.symbol_index,
            )
            _feed_integer(digest, b"relocation-type", relocation.type)
            _feed(
                digest,
                b"relocation-pcrel",
                b"1" if relocation.pcrel else b"0",
            )
            _feed_integer(digest, b"relocation-length", relocation.length)
            _feed_integer(digest, b"relocation-addend", relocation.addend)
            _feed_integer(
                digest,
                b"relocation-target-section",
                relocation.target_section_index,
            )
            _feed_integer(
                digest,
                b"relocation-minuend",
                relocation.minuend_index,
            )
            _feed_integer(
                digest,
                b"relocation-target-offset",
                relocation.target_offset,
            )
        _feed_integer(
            digest, b"data-in-code-count", len(section.data_in_code),
        )
        for region in section.data_in_code:
            _feed_integer(digest, b"data-in-code-offset", region.offset)
            _feed_integer(digest, b"data-in-code-length", region.length)
            _feed_integer(digest, b"data-in-code-kind", region.kind)


def _hash_packed_native_shape(
    digest,
    native: PackedNativeObject,
) -> None:
    """Packed equivalent of ``_hash_native_shape`` without materialization."""

    _feed_integer(digest, b"section-count", len(native.sections))
    _feed_integer(digest, b"symbol-count", len(native.symbols))
    for symbol in native.symbols:
        _feed_text(digest, b"symbol-name", symbol.name)
        _feed_integer(digest, b"symbol-section", symbol.section_index)
        _feed_integer(digest, b"symbol-offset", symbol.offset)
        _feed(digest, b"symbol-external", b"1" if symbol.external else b"0")
        _feed(
            digest,
            b"symbol-private-external",
            b"1" if symbol.private_external else b"0",
        )
        _feed(digest, b"symbol-weak-reference", b"1" if symbol.weak_reference else b"0")
    for section_index, section in enumerate(native.sections):
        _feed_text(digest, b"segment-name", section.segname)
        _feed_text(digest, b"section-name", section.sectname)
        _feed_integer(digest, b"section-flags", section.flags)
        _feed_integer(digest, b"section-align", section.align_log2)
        _feed_integer(digest, b"section-data-size", section.data_size)
        _feed_integer(digest, b"section-zerofill", section.zerofill_size)
        _feed_integer(
            digest, b"relocation-count", section.relocation_count,
        )
        for relocation in native.decoded_relocations(section_index):
            (
                offset,
                symbol_index,
                relocation_type,
                pcrel,
                length,
                addend,
                target_section_index,
                minuend_index,
                target_offset,
            ) = relocation
            _feed_integer(digest, b"relocation-offset", offset)
            _feed_integer(digest, b"relocation-symbol", symbol_index)
            _feed_integer(digest, b"relocation-type", relocation_type)
            _feed(digest, b"relocation-pcrel", b"1" if pcrel else b"0")
            _feed_integer(digest, b"relocation-length", length)
            _feed_integer(digest, b"relocation-addend", addend)
            _feed_integer(
                digest, b"relocation-target-section", target_section_index,
            )
            _feed_integer(digest, b"relocation-minuend", minuend_index)
            _feed_integer(
                digest, b"relocation-target-offset", target_offset,
            )
        _feed_integer(
            digest, b"data-in-code-count", len(section.data_in_code),
        )
        for offset, length, kind in section.data_in_code:
            _feed_integer(digest, b"data-in-code-offset", offset)
            _feed_integer(digest, b"data-in-code-length", length)
            _feed_integer(digest, b"data-in-code-kind", kind)


def _native_shape_is_patchable(native: NativeObject) -> bool:
    # ``link_relocatable_native`` rewrites a section-target field while
    # normalizing it.  Blindly copying the source bytes over that normalized
    # field would not reproduce a cold merge, so this shape stays cold.  A
    # stack-map table is rebuilt from every input's records (and may be
    # transcoded), never concatenated, so it stays cold too.
    return all(
        section.sectname != "__pcc_stackmaps"
        for section in native.sections
    ) and all(
        relocation.target_section_index is None
        for section in native.sections
        for relocation in section.relocations
    )


def _packed_native_shape_is_patchable(native: PackedNativeObject) -> bool:
    return all(
        section.sectname != "__pcc_stackmaps"
        for section in native.sections
    ) and all(
        relocation[6] is None
        for section_index, _section in enumerate(native.sections)
        for relocation in native.decoded_relocations(section_index)
    )


def _valid_link_options(
    entry: object,
    minos: object,
    identifier: object,
) -> bool:
    return (
        isinstance(entry, str)
        and isinstance(identifier, bytes)
        and isinstance(minos, tuple)
        and len(minos) == 2
        and all(isinstance(value, int) and not isinstance(value, bool)
                for value in minos)
    )


def _link_image_key(
    source_identity: str,
    objects: list[LinkInput],
    archives: list[bytes],
    *,
    entry: str,
    minos: tuple[int, int],
    identifier: bytes,
    semantic_identity: bytes = b"",
) -> str | None:
    if not _valid_link_options(entry, minos, identifier):
        return None
    # The final link writes the selected stack-map table format; an invalid
    # selection is left uncached for the link itself to reject.
    try:
        stack_map_format = precise_stackmap_runtime_format()
    except PreciseStackMapError:
        return None
    digest = hashlib.sha256()
    digest.update(_CACHE_SCHEMA)
    digest.update(_IMAGE_KEY_SCHEMA)
    _feed_integer(digest, b"stack-map-format", stack_map_format)
    _feed_text(digest, b"source-identity", source_identity)
    _feed_integer(digest, b"object-count", len(objects))
    for value in objects:
        if isinstance(value, PackedNativeObject):
            _feed(digest, b"native-object", value.encoded)
            continue
        native = _native_input(value)
        if native is not None:
            _feed(digest, b"native-object", encode_native_object(native))
        elif isinstance(value, bytes):
            _feed(digest, b"macho-object", value)
        else:
            return None
    _feed_integer(digest, b"archive-count", len(archives))
    for archive in archives:
        if not isinstance(archive, bytes):
            return None
        _feed(digest, b"archive", archive)
    _feed_text(digest, b"entry", entry)
    _feed_integer(digest, b"minos-major", minos[0])
    _feed_integer(digest, b"minos-minor", minos[1])
    _feed(digest, b"identifier", identifier)
    _feed(digest, b"semantic-identity", semantic_identity)
    return digest.hexdigest()


def _merged_layout_key(
    source_identity: str,
    objects: list[LinkInput],
    archives: list[bytes],
    *,
    semantic_identity: bytes = b"",
) -> str | None:
    # Semantic atom ordering/elimination changes the merged layout itself.
    # Exact-image reuse remains safe because its key includes the policy or
    # manifest digest, but prefix payload patching cannot reproduce that pass.
    if semantic_identity:
        return None
    digest = hashlib.sha256()
    digest.update(_CACHE_SCHEMA)
    digest.update(_LAYOUT_KEY_SCHEMA)
    _feed_text(digest, b"source-identity", source_identity)
    _feed_integer(digest, b"object-count", len(objects))
    saw_native = False
    saw_external = False
    for value in objects:
        if isinstance(value, PackedNativeObject):
            if saw_external or not _packed_native_shape_is_patchable(value):
                return None
            saw_native = True
            _feed(digest, b"native-shape-begin", b"")
            _hash_packed_native_shape(digest, value)
            _feed(digest, b"native-shape-end", b"")
            continue
        native = _native_input(value)
        if native is not None:
            # Prefix-only is what lets payload offsets be reconstructed without
            # reparsing external Mach-O sections on an incremental hit.
            if saw_external or not _native_shape_is_patchable(native):
                return None
            saw_native = True
            _feed(digest, b"native-shape-begin", b"")
            _hash_native_shape(digest, native)
            _feed(digest, b"native-shape-end", b"")
        elif isinstance(value, bytes):
            saw_external = True
            _feed(digest, b"stable-external-object", value)
        else:
            return None
    if not saw_native:
        return None
    _feed_integer(digest, b"archive-count", len(archives))
    for archive in archives:
        if not isinstance(archive, bytes):
            return None
        # Archive selection is already represented by the cached merged state;
        # a byte change must force a cold prepare and member re-selection.
        _feed(digest, b"stable-archive", archive)
    return digest.hexdigest()


def _align_up(value: int, align_log2: int) -> int:
    mask = (1 << align_log2) - 1
    return (value + mask) & ~mask


def _patch_merged_payloads(
    cached: NativeObject,
    objects: list[LinkInput],
) -> NativeObject:
    """Patch every current native-prefix payload into a cached merge."""

    section_indices = {
        (section.segname, section.sectname): index
        for index, section in enumerate(cached.sections)
    }
    if len(section_indices) != len(cached.sections):
        raise IncrementalLinkError("cached merge has duplicate section names")
    payloads = [bytearray(section.data) for section in cached.sections]
    cursors: dict[tuple[str, str], int] = {}
    saw_external = False
    saw_native = False
    for value in objects:
        packed = value if isinstance(value, PackedNativeObject) else None
        native = None if packed is not None else _native_input(value)
        if packed is not None:
            if saw_external or not _packed_native_shape_is_patchable(packed):
                raise IncrementalLinkError(
                    "native input is outside patchable prefix"
                )
            saw_native = True
            source_sections = packed.sections
        elif native is not None:
            if saw_external or not _native_shape_is_patchable(native):
                raise IncrementalLinkError(
                    "native input is outside patchable prefix"
                )
            saw_native = True
            source_sections = native.sections
        else:
            saw_external = True
            continue
        for section_index, section in enumerate(source_sections):
            key = (section.segname, section.sectname)
            index = section_indices.get(key)
            if index is None:
                raise IncrementalLinkError(
                    f"cached merge is missing section {key!r}"
                )
            base = _align_up(cursors.get(key, 0), section.align_log2)
            end = base + section.vm_size
            cached_section = cached.sections[index]
            if section.zerofill_size:
                if end > cached_section.vm_size:
                    raise IncrementalLinkError(
                        f"cached zerofill section {key!r} is too small"
                    )
            else:
                if end > len(payloads[index]):
                    raise IncrementalLinkError(
                        f"cached content section {key!r} is too small"
                    )
                source_data = (
                    packed.section_data(section_index)
                    if packed is not None else section.data
                )
                payloads[index][base:end] = source_data
            cursors[key] = end
    if not saw_native:
        raise IncrementalLinkError("incremental merge has no native prefix")

    sections = tuple(
        replace(section, data=bytes(payloads[index]))
        for index, section in enumerate(cached.sections)
    )
    try:
        return NativeObject(sections, cached.symbols)
    except NativeObjectError as exc:
        raise IncrementalLinkError(
            f"patched merged object is invalid: {exc}"
        ) from exc


def _checked_cache_read(path: Path) -> bytes | None:
    checksum_path = path.with_name(path.name + ".sha256")
    try:
        if path.stat().st_size > _MAX_CACHE_PAYLOAD:
            return None
        if checksum_path.stat().st_size > 128:
            return None
        expected = checksum_path.read_text(encoding="ascii").strip().lower()
        if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
            return None
        payload = path.read_bytes()
    except (OSError, UnicodeError):
        return None
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected):
        return None
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checksum_path = path.with_name(path.name + ".sha256")
    temporaries: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            payload_tmp = Path(stream.name)
            temporaries.append(payload_tmp)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        checksum = hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n"
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=checksum_path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            checksum_tmp = Path(stream.name)
            temporaries.append(checksum_tmp)
            stream.write(checksum)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(payload_tmp, path)
        temporaries.remove(payload_tmp)
        os.replace(checksum_tmp, checksum_path)
        temporaries.remove(checksum_tmp)
    finally:
        for temporary in temporaries:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class IncrementalMachOLinker:
    """Persistent exact-image and compatible-merged-state cache."""

    def __init__(self, cache_dir: str | Path, source_identity: str):
        identity = str(source_identity or "").strip()
        if not identity:
            raise IncrementalLinkError("linker source identity must not be empty")
        self.cache_dir = Path(cache_dir)
        self.source_identity = identity
        self.stats = IncrementalLinkStats()

    def _cache_path(self, kind: str, key: str, suffix: str) -> Path:
        return self.cache_dir / kind / key[:2] / (key + suffix)

    def _store_exact(
        self,
        path: Path,
        payload: bytes,
        *,
        replace_valid: bool = False,
    ) -> None:
        existing = _checked_cache_read(path)
        if existing is not None:
            if existing == payload:
                return
            if not replace_valid:
                raise IncrementalLinkError(
                    f"content-addressed cache conflict at {path}"
                )
        try:
            _atomic_write(path, payload)
        except OSError:
            # Cache availability is never a correctness dependency.
            return

    def _store_layout(
        self,
        path: Path,
        payload: bytes,
        *,
        replace_valid: bool = False,
    ) -> None:
        # A layout key intentionally omits native section bytes.  Any valid
        # content variant is a usable baseline because a hit patches *all*
        # native-prefix payload ranges before finalization.
        if _checked_cache_read(path) is not None and not replace_valid:
            return
        try:
            _atomic_write(path, payload)
        except OSError:
            return

    def probe_assembly_cache(
        self,
        assembly: str,
        *,
        packed: bool = False,
    ) -> tuple[Path, NativeObject | PackedNativeObject | None, bool]:
        """(cache path, cached object or None, replace-corrupt-entry flag).

        Split out of ``native_object_from_assembly`` so a driver can probe
        every input first and assemble only the misses in parallel worker
        processes; a hit is counted here, a miss is counted at store time.
        """
        if not isinstance(assembly, str):
            raise IncrementalLinkError("assembly cache input must be text")
        digest = hashlib.sha256()
        digest.update(_CACHE_SCHEMA)
        digest.update(_ASSEMBLY_KEY_SCHEMA)
        _feed_text(digest, b"source-identity", self.source_identity)
        _feed(digest, b"assembly", assembly.encode("utf-8"))
        key = digest.hexdigest()
        path = self._cache_path("native", key, ".pco")
        payload = _checked_cache_read(path)
        if payload is not None:
            try:
                native = (
                    decode_packed_native_object(payload)
                    if packed else decode_native_object(payload)
                )
            except NativeObjectError:
                return path, None, True
            else:
                self.stats.assembly_hits += 1
                return path, native, False
        return path, None, False

    def store_assembled_native_object(
        self,
        path: Path,
        native: NativeObject | PackedNativeObject,
        *,
        replace_valid: bool = False,
        encoded: bytes | None = None,
    ) -> None:
        self.stats.assembly_misses += 1
        if encoded is None:
            encoded = (
                native.encoded
                if isinstance(native, PackedNativeObject)
                else encode_native_object(native)
            )
        self._store_exact(path, encoded, replace_valid=replace_valid)

    def native_object_from_assembly(
        self,
        assembly: str,
        assemble: Callable[[str], object],
    ) -> NativeObject:
        path, cached, replace_cached_native = self.probe_assembly_cache(
            assembly
        )
        if cached is not None:
            return cached

        built = assemble(assembly)
        if isinstance(built, NativeObject):
            native = built
        else:
            try:
                sections, undefined = built
            except (TypeError, ValueError) as exc:
                raise IncrementalLinkError(
                    "assembler must return NativeObject or (sections, undefined)"
                ) from exc
            native = NativeObject.from_sections(sections, undefined=undefined)
        self.store_assembled_native_object(
            path,
            native,
            replace_valid=replace_cached_native,
        )
        return native

    def link(
        self,
        objects: list[LinkInput],
        *,
        archives: list[bytes] = (),
        entry: str = "_main",
        minos: tuple[int, int] = (12, 0),
        identifier: bytes = b"pcc-linked",
        semantic_identity: bytes = b"",
        prepare: Callable[..., NativeObject] = prepare_executable_object,
        finalize: Callable[..., bytes] = link_prepared_executable,
        validate: Callable[[bytes], object] | None = None,
    ) -> bytes:
        if not isinstance(semantic_identity, bytes):
            raise IncrementalLinkError("semantic identity must be bytes")
        if len(semantic_identity) > 256:
            raise IncrementalLinkError("semantic identity is unreasonably large")
        object_list = list(objects)
        archive_list = list(archives)
        image_key = _link_image_key(
            self.source_identity,
            object_list,
            archive_list,
            entry=entry,
            minos=minos,
            identifier=identifier,
            semantic_identity=semantic_identity,
        )
        image_path = (
            self._cache_path("image", image_key, ".macho")
            if image_key is not None else None
        )
        replace_cached_image = False
        if image_path is not None:
            cached_image = _checked_cache_read(image_path)
            if cached_image is not None:
                try:
                    validate_executable_imports(cached_image)
                    if validate is not None:
                        validate(cached_image)
                except MemoryError:
                    raise
                except Exception:
                    replace_cached_image = True
                else:
                    self.stats.image_hits += 1
                    return cached_image
            self.stats.image_misses += 1
        else:
            self.stats.uncacheable_links += 1

        layout_key = _merged_layout_key(
            self.source_identity,
            object_list,
            archive_list,
            semantic_identity=semantic_identity,
        )
        layout_path = (
            self._cache_path("merged", layout_key, ".pco")
            if layout_key is not None else None
        )
        merged: NativeObject | None = None
        reused_merge = False
        replace_cached_layout = False
        if layout_path is not None:
            cached_merged = _checked_cache_read(layout_path)
            if cached_merged is not None:
                try:
                    merged = _patch_merged_payloads(
                        decode_native_object(cached_merged), object_list,
                    )
                except (IncrementalLinkError, NativeObjectError):
                    merged = None
                    replace_cached_layout = True
                else:
                    reused_merge = True
                    self.stats.merged_hits += 1
            if merged is None:
                self.stats.merged_misses += 1

        if merged is not None:
            try:
                image = finalize(
                    merged,
                    entry=entry,
                    minos=minos,
                    identifier=identifier,
                )
                if not isinstance(image, bytes):
                    raise IncrementalLinkError(
                        "prepared linker returned a non-bytes image"
                    )
                if validate is not None:
                    validate(image)
            except MemoryError:
                raise
            except Exception:
                # A stale-but-decodable state or an unexpectedly narrower
                # proof boundary must not turn a valid cold link into failure.
                self.stats.incremental_fallbacks += 1
                reused_merge = False
                replace_cached_layout = True
                merged = None

        if merged is None:
            merged = prepare(object_list, archives=archive_list)
            image = finalize(
                merged,
                entry=entry,
                minos=minos,
                identifier=identifier,
            )
            if not isinstance(image, bytes):
                raise IncrementalLinkError(
                    "prepared linker returned a non-bytes image"
                )
            if validate is not None:
                validate(image)

        if layout_path is not None and not reused_merge:
            self._store_layout(
                layout_path,
                encode_native_object(merged),
                replace_valid=replace_cached_layout,
            )
        if image_path is not None:
            self._store_exact(
                image_path,
                image,
                replace_valid=replace_cached_image,
            )
        return image


def incremental_link_session_from_environment(
    source_root: str | Path,
) -> IncrementalMachOLinker | None:
    """Create the default session, sharing bootstrap's object-cache lifetime."""

    configured = str(os.environ.get(INCREMENTAL_CACHE_ENV, "") or "")
    normalized = configured.strip().lower()
    if normalized:
        if normalized in _FALSE_VALUES:
            return None
        if normalized not in _TRUE_VALUES:
            raise IncrementalLinkError(
                f"invalid {INCREMENTAL_CACHE_ENV} value {configured!r}"
            )
    else:
        object_cache = str(os.environ.get(_OBJECT_CACHE_ENV, "") or "")
        if object_cache.strip().lower() in _FALSE_VALUES:
            return None

    explicit_dir = str(
        os.environ.get(INCREMENTAL_CACHE_DIR_ENV, "") or ""
    ).strip()
    if explicit_dir:
        cache_dir = Path(explicit_dir).expanduser()
    else:
        object_dir = str(
            os.environ.get(_OBJECT_CACHE_DIR_ENV, "") or ""
        ).strip()
        if object_dir:
            cache_dir = Path(object_dir).expanduser() / "macho-incremental-v1"
        else:
            cache_dir = (
                Path("~").expanduser()
                / ".cache"
                / "pcc"
                / "macho-incremental-link"
            )
    try:
        identity = macho_linker_source_identity(source_root)
    except OSError:
        # Source identity is mandatory for reuse, but failure to identify it
        # must leave the ordinary owned linker available.
        return None
    runtime_digest = hashlib.sha256()
    runtime_digest.update(b"pcc.macho-linker-runtime.v1\0")
    _feed_text(runtime_digest, b"source", identity)
    _feed_text(
        runtime_digest,
        b"implementation",
        str(getattr(sys.implementation, "name", "")),
    )
    _feed_text(
        runtime_digest,
        b"cache-tag",
        str(getattr(sys.implementation, "cache_tag", "")),
    )
    _feed_text(
        runtime_digest,
        b"version",
        ".".join(str(value) for value in sys.version_info[:3]),
    )
    return IncrementalMachOLinker(cache_dir, runtime_digest.hexdigest())


__all__ = [
    "INCREMENTAL_CACHE_DIR_ENV",
    "INCREMENTAL_CACHE_ENV",
    "IncrementalLinkError",
    "IncrementalLinkStats",
    "IncrementalMachOLinker",
    "incremental_link_session_from_environment",
]
