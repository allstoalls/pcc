from __future__ import annotations

"""AArch64 Darwin memory/opcode helpers for the self backend."""

from . import BackendUnavailable
from .arm64_encode import (
    EMITTED_INSTRUCTION_CALL,
    EMITTED_INSTRUCTION_SCALAR,
    EMITTED_INSTRUCTION_UNSCALED,
    EMITTED_INSTRUCTION_MOVE,
    EncodeError,
    STRUCTURED_RELOCATION_GOT_LOAD_PAGE21,
    STRUCTURED_RELOCATION_GOT_LOAD_PAGEOFF12,
    STRUCTURED_RELOCATION_PAGE21,
    STRUCTURED_RELOCATION_PAGEOFF12,
    encode_emitted_addsub_immediate_parts,
    encode_emitted_addsub_register_parts,
    encode_emitted_branch_base_parts,
    encode_emitted_compare_immediate_parts,
    encode_emitted_compare_register_parts,
    encode_emitted_cset_parts,
    encode_emitted_frame_pair_parts,
    encode_emitted_add_pageoff_parts,
    encode_emitted_adrp_parts,
    encode_emitted_ldr_got_pageoff_parts,
    encode_emitted_load_store_parts,
    encode_emitted_move_register_parts,
    encode_emitted_movewide_parts,
)
from .self_backend_ir import TypeDesc
from .self_backend_value_arena import CompilerIntArena


DIRECT_INSTRUCTION_PLACEHOLDER = "  pcc_direct_instruction"
_DIRECT_INSTRUCTION_RECORDS: CompilerIntArena | None = None
_DIRECT_INSTRUCTION_SYMBOL_NAMES: list[str] = []
_DIRECT_INSTRUCTION_SYMBOL_IDS: dict[str, int] = {}


def begin_direct_instruction_capture() -> None:
    """Start one process-local structured PCO instruction stream."""

    global _DIRECT_INSTRUCTION_RECORDS
    global _DIRECT_INSTRUCTION_SYMBOL_NAMES
    global _DIRECT_INSTRUCTION_SYMBOL_IDS
    if _DIRECT_INSTRUCTION_RECORDS is not None:
        raise BackendUnavailable("direct instruction capture is already active")
    _DIRECT_INSTRUCTION_RECORDS = CompilerIntArena()
    _DIRECT_INSTRUCTION_SYMBOL_NAMES = []
    _DIRECT_INSTRUCTION_SYMBOL_IDS = {}


def borrow_direct_instruction_records() -> CompilerIntArena:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is None:
        raise RuntimeError("direct instruction capture is not active")
    # Keep the module-global owner live while the caller consumes this
    # borrowed arena.  Clearing the global here frees the arena under pcc1
    # before the borrowed local can be returned; CPython masks that bug by
    # retaining ordinary local assignments.
    return records


def borrow_direct_instruction_symbol_names() -> list[str]:
    if _DIRECT_INSTRUCTION_RECORDS is None:
        raise RuntimeError("direct instruction capture is not active")
    return _DIRECT_INSTRUCTION_SYMBOL_NAMES


def end_direct_instruction_capture() -> None:
    global _DIRECT_INSTRUCTION_RECORDS
    global _DIRECT_INSTRUCTION_SYMBOL_NAMES
    global _DIRECT_INSTRUCTION_SYMBOL_IDS
    if _DIRECT_INSTRUCTION_RECORDS is not None:
        _DIRECT_INSTRUCTION_RECORDS.close()
    _DIRECT_INSTRUCTION_RECORDS = None
    _DIRECT_INSTRUCTION_SYMBOL_NAMES = []
    _DIRECT_INSTRUCTION_SYMBOL_IDS = {}


def require_direct_instruction_capture_idle() -> None:
    if _DIRECT_INSTRUCTION_RECORDS is not None:
        raise BackendUnavailable("direct instruction capture is already active")


def direct_instruction_capture_active() -> bool:
    return _DIRECT_INSTRUCTION_RECORDS is not None


def emitted_memory_instruction_line(
    mnemonic: str,
    register: str,
    base: str,
    offset: int = 0,
) -> str:
    """Return exact ASM text or one constant placeholder plus a packed word."""

    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        try:
            word = encode_emitted_load_store_parts(
                mnemonic,
                register,
                base,
                offset,
            )
        except EncodeError:
            pass
        else:
            family = (
                EMITTED_INSTRUCTION_UNSCALED
                if mnemonic in ("ldur", "stur", "ldurb", "sturb", "ldurh", "sturh")
                else EMITTED_INSTRUCTION_SCALAR
            )
            records.append4(word, family, 0, -1)
            return DIRECT_INSTRUCTION_PLACEHOLDER
    suffix = "" if offset == 0 else ", #" + str(offset)
    return "  " + mnemonic + " " + register + ", [" + base + suffix + "]"


def emitted_move_register_line(destination: str, source: str) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        word = encode_emitted_move_register_parts(destination, source)
        records.append4(word, EMITTED_INSTRUCTION_MOVE, 0, -1)
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return "  mov " + destination + ", " + source


def emitted_movewide_instruction_line(
    mnemonic: str,
    destination: str,
    immediate: int,
    shift: int = 0,
    explicit_shift: bool = False,
) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        word = encode_emitted_movewide_parts(
            mnemonic,
            destination,
            immediate,
            shift,
        )
        records.append4(word, EMITTED_INSTRUCTION_MOVE, 0, -1)
        return DIRECT_INSTRUCTION_PLACEHOLDER
    suffix = "" if shift == 0 and not explicit_shift else ", lsl #" + str(shift)
    return (
        "  "
        + mnemonic
        + " "
        + destination
        + ", #"
        + str(immediate)
        + suffix
    )


def emitted_addsub_register_line(
    mnemonic: str,
    destination: str,
    left: str,
    right: str,
) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        word = encode_emitted_addsub_register_parts(
            mnemonic,
            destination,
            left,
            right,
        )
        records.append4(word, EMITTED_INSTRUCTION_SCALAR, 0, -1)
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return (
        "  "
        + mnemonic
        + " "
        + destination
        + ", "
        + left
        + ", "
        + right
    )


def emitted_addsub_immediate_line(
    mnemonic: str,
    destination: str,
    left: str,
    immediate: int,
) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        word = encode_emitted_addsub_immediate_parts(
            mnemonic,
            destination,
            left,
            immediate,
        )
        records.append4(word, EMITTED_INSTRUCTION_SCALAR, 0, -1)
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return (
        "  "
        + mnemonic
        + " "
        + destination
        + ", "
        + left
        + ", #"
        + str(immediate)
    )


def emitted_compare_register_line(left: str, right: str) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        records.append4(
            encode_emitted_compare_register_parts(left, right),
            EMITTED_INSTRUCTION_SCALAR,
            0,
            -1,
        )
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return "  cmp " + left + ", " + right


def emitted_compare_immediate_line(left: str, immediate: int) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        records.append4(
            encode_emitted_compare_immediate_parts(left, immediate),
            EMITTED_INSTRUCTION_SCALAR,
            0,
            -1,
        )
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return "  cmp " + left + ", #" + str(immediate)


def emitted_cset_line(destination: str, condition: str) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        records.append4(
            encode_emitted_cset_parts(destination, condition),
            EMITTED_INSTRUCTION_SCALAR,
            0,
            -1,
        )
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return "  cset " + destination + ", " + condition


def emitted_direct_call_line(target: str) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        symbol_id = _capture_direct_symbol(target)
        records.append4(0, EMITTED_INSTRUCTION_CALL, 0, symbol_id)
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return "  bl " + target


def emitted_branch_line(
    mnemonic: str,
    target: str,
    register: str = "",
) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        target_id = _capture_direct_symbol(target)
        width = 26 if mnemonic == "b" else 19
        records.append4(
            encode_emitted_branch_base_parts(mnemonic, register),
            EMITTED_INSTRUCTION_SCALAR,
            -width,
            target_id,
        )
        return DIRECT_INSTRUCTION_PLACEHOLDER
    if mnemonic == "b" or mnemonic.startswith("b."):
        return "  " + mnemonic + " " + target
    return "  " + mnemonic + " " + register + ", " + target


def emitted_fixed_instruction_line(mnemonic: str) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        if mnemonic == "ret":
            word = 0xD65F03C0
        elif mnemonic == "nop":
            word = 0xD503201F
        elif mnemonic == "paciasp":
            word = 0xD503233F
        elif mnemonic == "autiasp":
            word = 0xD50323BF
        else:
            raise EncodeError("unsupported emitted fixed instruction " + mnemonic)
        records.append4(word, EMITTED_INSTRUCTION_SCALAR, 0, -1)
        return DIRECT_INSTRUCTION_PLACEHOLDER
    return "  " + mnemonic


def emitted_frame_pair_line(load: bool) -> str:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        records.append4(
            encode_emitted_frame_pair_parts(load),
            EMITTED_INSTRUCTION_SCALAR,
            0,
            -1,
        )
        return DIRECT_INSTRUCTION_PLACEHOLDER
    if load:
        return "  ldp x29, x30, [sp], #16"
    return "  stp x29, x30, [sp, #-16]!"


def _capture_direct_symbol(symbol: str) -> int:
    if symbol in _DIRECT_INSTRUCTION_SYMBOL_IDS:
        return _DIRECT_INSTRUCTION_SYMBOL_IDS[symbol]
    symbol_id = len(_DIRECT_INSTRUCTION_SYMBOL_NAMES)
    _DIRECT_INSTRUCTION_SYMBOL_IDS[symbol] = symbol_id
    _DIRECT_INSTRUCTION_SYMBOL_NAMES.append(symbol)
    return symbol_id


def emitted_global_address_lines(
    register: str,
    symbol: str,
    got: bool,
) -> list[str]:
    records = _DIRECT_INSTRUCTION_RECORDS
    if records is not None:
        symbol_id = _capture_direct_symbol(symbol)
        records.append4(
            encode_emitted_adrp_parts(register),
            EMITTED_INSTRUCTION_SCALAR,
            (
                STRUCTURED_RELOCATION_GOT_LOAD_PAGE21
                if got
                else STRUCTURED_RELOCATION_PAGE21
            ),
            symbol_id,
        )
        records.append4(
            (
                encode_emitted_ldr_got_pageoff_parts(register, register)
                if got
                else encode_emitted_add_pageoff_parts(register, register)
            ),
            EMITTED_INSTRUCTION_SCALAR,
            (
                STRUCTURED_RELOCATION_GOT_LOAD_PAGEOFF12
                if got
                else STRUCTURED_RELOCATION_PAGEOFF12
            ),
            symbol_id,
        )
        return [
            DIRECT_INSTRUCTION_PLACEHOLDER,
            DIRECT_INSTRUCTION_PLACEHOLDER,
        ]
    if got:
        return [
            "  adrp " + register + ", " + symbol + "@GOTPAGE",
            "  ldr " + register + ", [" + register + ", " + symbol + "@GOTPAGEOFF]",
        ]
    return [
        "  adrp " + register + ", " + symbol + "@PAGE",
        "  add " + register + ", " + register + ", " + symbol + "@PAGEOFF",
    ]


def stack_load_op(value_type: TypeDesc) -> str:
    if value_type.is_ptr or value_type.is_fp or (value_type.is_int and value_type.width > 16):
        return "ldur"
    if value_type.is_int and value_type.width <= 8:
        return "ldurb"
    if value_type.is_int and value_type.width <= 16:
        return "ldurh"
    return "ldur"


def stack_store_op(value_type: TypeDesc) -> str:
    if value_type.is_ptr or value_type.is_fp or (value_type.is_int and value_type.width > 16):
        return "stur"
    if value_type.is_int and value_type.width <= 8:
        return "sturb"
    if value_type.is_int and value_type.width <= 16:
        return "sturh"
    return "stur"


def mem_load_op(value_type: TypeDesc) -> str:
    if value_type.is_ptr or (value_type.is_int and value_type.width > 32):
        return "ldr"
    if value_type.is_int and value_type.width <= 8:
        return "ldrb"
    if value_type.is_int and value_type.width <= 16:
        return "ldrh"
    return "ldr"


def mem_store_op(value_type: TypeDesc) -> str:
    if value_type.is_ptr or (value_type.is_int and value_type.width > 32):
        return "str"
    if value_type.is_int and value_type.width <= 8:
        return "strb"
    if value_type.is_int and value_type.width <= 16:
        return "strh"
    return "str"


def chunk_load_op(size: int, *, stack: bool) -> str:
    if size == 8:
        return "ldur" if stack else "ldr"
    if size == 4:
        return "ldur" if stack else "ldr"
    if size == 2:
        return "ldurh" if stack else "ldrh"
    if size == 1:
        return "ldurb" if stack else "ldrb"
    raise BackendUnavailable(f"self backend does not support aggregate chunk load size {size}")


def chunk_store_op(size: int, *, stack: bool) -> str:
    if size == 8:
        return "stur" if stack else "str"
    if size == 4:
        return "stur" if stack else "str"
    if size == 2:
        return "sturh" if stack else "strh"
    if size == 1:
        return "sturb" if stack else "strb"
    raise BackendUnavailable(f"self backend does not support aggregate chunk store size {size}")


def aggregate_copy_chunks(size: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    offset = 0
    remaining = size
    for chunk_size in (8, 4, 2, 1):
        while remaining >= chunk_size:
            chunks.append((offset, chunk_size))
            offset += chunk_size
            remaining -= chunk_size
    return chunks
