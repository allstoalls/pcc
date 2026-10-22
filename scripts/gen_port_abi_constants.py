#!/usr/bin/env python3
"""Generate the pcc-Python port's ABI constants from the C runtime headers.

The ports under ``pcc/runtime/py/`` read object fields through byte
offsets. Those offsets are currently hand-written literals in 140+ places, so
a C-side layout change reaches the port only if a human notices
(ARCH-P2-PORT-ABI-AUTOGEN). This generator makes the C headers the single
source of truth: it compiles a probe with the host cc, reads real
``offsetof``/``sizeof``/enum values, and writes
``pcc/runtime/py/py_abi_constants.py`` and the matching static-export
metadata consumed by single-object runtime builds.

Usage:
    uv run python scripts/gen_port_abi_constants.py            # write
    uv run python scripts/gen_port_abi_constants.py --check    # verify only

``--check`` is what CI/tests use: it regenerates in memory and fails if the
committed file is stale, so a C layout change cannot land silently.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


def repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


REPO = repo_root()
RUNTIME = REPO / "pcc" / "runtime"
OUTPUT = RUNTIME / "py" / "py_abi_constants.py"
EXPORTS_OUTPUT = REPO / "pcc" / "frontends" / "python" / "codegen" / "port_abi_exports.py"

# What the ports need. Kept in step with
# tests/python/test_runtime_layout_contract.py, which pins these same
# structures; this generator emits the values, that test proves they are real.
STRUCT_FIELDS: dict[str, tuple[str, ...]] = {
    "PyObjectHeader": ("refcount", "type_tag", "flags"),
    "PyIntObject": ("sign", "ndigits", "digits"),
    "PyFloatObject": ("value",),
    "PyComplexObject": ("real", "imag"),
    "PyBytesObject": ("byte_len", "data"),
    "PyByteArrayObject": ("byte_len", "data"),
    "PyMemoryViewObject": ("base",),
    "PyStrObject": ("byte_len", "cp_len", "hash", "data"),
    "PyListObject": ("length", "capacity", "items"),
    "PyTupleObject": ("len", "items"),
    "PyDictObject": ("size", "capacity", "indices", "entries", "entries_used"),
    "DictEntry": ("hash", "key", "value"),
    "PyClassObject": (
        "name", "n_bases", "bases", "n_mro", "mro", "n_methods",
        "methods", "n_fields", "field_names", "instance_size",
        "type_tag_alloc", "del_method", "attrs", "metaclass",
    ),
    "PyClassMethod": ("name", "func", "name_hash", "name_length"),
    "PyInstanceObject": ("cls", "fields"),
    "PyPropertyObject": ("fget", "fset", "fdel"),
    "PyClassMethodObject": ("func",),
    "PyStaticMethodObject": ("func",),
    "PyVThreadChannelEndpointObject": ("kind", "core", "closed"),
    "PyVThreadChannelCoreObject": (
        "kind", "capacity", "length", "head", "tail", "sender_count",
        "receiver_closed", "oneshot", "oneshot_sent", "send_head",
        "send_tail", "recv_head", "recv_tail", "flags", "items",
    ),
    "PyVirtualThreadObject": (
        "continuation", "result", "state", "queued", "pinned",
        "timer_entry", "io_entry", "exception", "outcome", "join_waiters",
        "join_wait_tail", "join_entry", "join_target", "wait_kind",
        "cancel_requested", "channel_owner_a", "channel_owner_b",
        "channel_arm_a", "channel_arm_b", "channel_value",
        "channel_status", "channel_index", "identity",
    ),
}

SIZEOF_STRUCTS: tuple[str, ...] = (
    "PyObjectHeader",
    "DictEntry",
    "PyClassObject",
    "PyClassMethod",
    "PyListObject",
    "PyStrObject",
    "PyTupleObject",
    "PyDictObject",
    "PyInstanceObject",
    "PyPropertyObject",
    "PyClassMethodObject",
    "PyStaticMethodObject",
    "PyVThreadChannelEndpointObject",
    "PyVThreadChannelCoreObject",
    "PyVirtualThreadObject",
)

SCALAR_SIZES: dict[str, str] = {
    "C_POINTER": "void *",
}

TYPE_TAG_HEADERS: tuple[Path, ...] = (
    RUNTIME / "include" / "py_runtime.h",
    RUNTIME / "src" / "py_internal.h",
)

_ENUM_TYPE_TAG_RE = re.compile(r"^\s*(PY_TYPE_[A-Z0-9_]+)\s*=", re.MULTILINE)
_DEFINE_TYPE_TAG_RE = re.compile(
    r"^\s*#define\s+(PY_TYPE_[A-Z0-9_]+)\b", re.MULTILINE
)

FLAGS: tuple[str, ...] = (
    "PY_FLAG_FINALIZED", "PY_FLAG_GC_TRACKED", "PY_FLAG_IMMORTAL",
    "PY_FLAG_GC_MALLOC_ALLOC", "PY_FLAG_GC_PINNED",
    "PY_FLAG_FUNC_AUTO_PARK", "PY_FLAG_FUNC_TRANSPARENT_CALL",
    "PY_FLAG_GEN_SOURCE", "PY_FLAG_FUNC_CONTINUATION_FACTORY",
    "PY_FLAG_EXC_SUPPRESS_CONTEXT", "PY_FLAG_EXC_UNICODE_PAYLOAD",
    "PY_FLAG_EXC_OS_PAYLOAD", "PY_FLAG_FUNC_BUILTIN", "PY_FLAG_CLASS_ATTRS_EXPOSED",
)

RUNTIME_ABI_CONSTANTS: tuple[str, ...] = (
    "PY_OBJ_CMP_UNORDERED",
    "PY_EXC_N_BUILTIN",
    "PCC_VTHREAD_WAIT_CHANNEL_SEND",
    "PCC_VTHREAD_WAIT_CHANNEL_RECV",
    "PCC_VTHREAD_WAIT_CHANNEL_SELECT2",
    "PCC_VTHREAD_CHANNEL_KIND_CORE",
    "PCC_VTHREAD_CHANNEL_KIND_SENDER",
    "PCC_VTHREAD_CHANNEL_KIND_RECEIVER",
    "PCC_VTHREAD_CHANNEL_MODE_MPSC",
    "PCC_VTHREAD_CHANNEL_MODE_ONESHOT",
    "PCC_VTHREAD_CHANNEL_SEND_RECEIVER_CLOSED",
    "PCC_VTHREAD_CHANNEL_SEND_ACCEPTED",
    "PCC_VTHREAD_CHANNEL_SEND_ERROR",
    "PCC_VTHREAD_CHANNEL_RECV_VALUE",
    "PCC_VTHREAD_CHANNEL_RECV_SENDER_CLOSED",
    "PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED",
    "PCC_VTHREAD_CHANNEL_SELECT_LEFT",
    "PCC_VTHREAD_CHANNEL_SELECT_RIGHT",
    "PCC_VTHREAD_CHANNEL_MAX_CAPACITY",
)


# A field named ``size`` and ``sizeof(struct)`` otherwise become the nearly
# indistinguishable FOO_SIZE_OFFSET and FOO_SIZE.  Give ambiguous field names
# an explicit semantic name at the generated boundary.
FIELD_CONSTANT_NAMES: dict[str, str] = {
    "PyDictObject.size": "PYDICTOBJECT_ITEM_COUNT_OFFSET",
}


def _offset_constant_name(key: str) -> str:
    return FIELD_CONSTANT_NAMES.get(
        key,
        key.replace(".", "_").upper() + "_OFFSET",
    )


def _type_tags_from_headers() -> tuple[str, ...]:
    """Return every public object tag declared by the runtime headers.

    The inventory is deliberately discovered instead of curated. A new C tag
    must therefore compile into both generated outputs or make generation
    fail; it cannot be omitted because a second list was not updated.
    """
    names: list[str] = []
    seen: set[str] = set()
    for path in TYPE_TAG_HEADERS:
        source = path.read_text(encoding="utf-8")
        for pattern in (_ENUM_TYPE_TAG_RE, _DEFINE_TYPE_TAG_RE):
            for match in pattern.finditer(source):
                name = match.group(1)
                if name not in seen:
                    seen.add(name)
                    names.append(name)
    if not names:
        raise RuntimeError("no PY_TYPE_* declarations found in runtime headers")
    return tuple(names)


def _constant_groups(
    abi: dict[str, dict[str, int]],
) -> tuple[tuple[str, list[tuple[str, int]]], ...]:
    offsets = [
        (_offset_constant_name(key), abi["offsets"][key])
        for key in sorted(abi["offsets"])
    ]
    sizes = [
        (key.upper() + "_SIZE", abi["sizes"][key])
        for key in sorted(abi["sizes"])
    ]
    tags = [
        (key, abi["tags"][key])
        for key in sorted(abi["tags"], key=lambda k: (abi["tags"][k], k))
    ]
    flags = [
        (key, abi["flags"][key])
        for key in sorted(abi["flags"], key=lambda k: (abi["flags"][k], k))
    ]
    runtime = [(key, abi["runtime"][key]) for key in RUNTIME_ABI_CONSTANTS]
    return (
        ("struct field offsets", offsets),
        ("struct sizes", sizes),
        ("type tags (C runtime ABI)", tags),
        ("header flags", flags),
        ("runtime ABI constants", runtime),
    )


def _probe_source() -> str:
    lines: list[str] = []
    for struct, fields in STRUCT_FIELDS.items():
        for field in fields:
            lines.append(
                f'    printf("O {struct} {field} %zu\\n", '
                f"offsetof({struct}, {field}));"
            )
    for struct in SIZEOF_STRUCTS:
        lines.append(f'    printf("S {struct} %zu\\n", sizeof({struct}));')
    for name, c_type in SCALAR_SIZES.items():
        lines.append(f'    printf("S {name} %zu\\n", sizeof({c_type}));')
    for tag in _type_tags_from_headers():
        lines.append(f'    printf("T {tag} %lld\\n", (long long){tag});')
    for flag in FLAGS:
        lines.append(f'    printf("F {flag} %lld\\n", (long long){flag});')
    for name in RUNTIME_ABI_CONSTANTS:
        lines.append(f'    printf("A {name} %lld\\n", (long long){name});')
    return textwrap.dedent(
        """
        #include "py_internal.h"
        #include <stddef.h>
        #include <stdio.h>

        int main(void) {
        %s
            return 0;
        }
        """
    ).lstrip() % "\n".join(lines)


def read_abi() -> dict[str, dict[str, int]]:
    cc = os.environ.get("CC", "cc")
    with tempfile.TemporaryDirectory(prefix="pcc-abi-gen-") as tmp:
        src = Path(tmp) / "abi_probe.c"
        exe = Path(tmp) / "abi_probe.out"
        src.write_text(_probe_source(), encoding="utf-8")
        build = subprocess.run(
            [
                cc, "-std=c11",
                f"-I{RUNTIME / 'include'}", f"-I{RUNTIME / 'src'}",
                str(src), "-o", str(exe),
            ],
            capture_output=True, text=True, timeout=180,
        )
        if build.returncode != 0:
            raise SystemExit("abi probe failed to build:\n" + build.stdout + build.stderr)
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            raise SystemExit("abi probe failed to run:\n" + run.stderr)

    offsets: dict[str, int] = {}
    sizes: dict[str, int] = {}
    tags: dict[str, int] = {}
    flags: dict[str, int] = {}
    runtime: dict[str, int] = {}
    for line in run.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "O":
            offsets[f"{parts[1]}.{parts[2]}"] = int(parts[3])
        elif parts[0] == "S":
            sizes[parts[1]] = int(parts[2])
        elif parts[0] == "T":
            tags[parts[1]] = int(parts[2])
        elif parts[0] == "F":
            flags[parts[1]] = int(parts[2])
        elif parts[0] == "A":
            runtime[parts[1]] = int(parts[2])
    return {
        "offsets": offsets,
        "sizes": sizes,
        "tags": tags,
        "flags": flags,
        "runtime": runtime,
    }


def _constant_items(abi: dict[str, dict[str, int]]) -> list[tuple[str, int]]:
    return [item for _heading, items in _constant_groups(abi) for item in items]


def render(abi: dict[str, dict[str, int]]) -> str:
    out: list[str] = [
        '"""Object ABI constants for the pcc-Python runtime ports.',
        "",
        "GENERATED by scripts/gen_port_abi_constants.py from the C runtime",
        "headers. Do not edit by hand: run the generator, or the --check mode in",
        "tests/python/test_port_abi_constants.py will fail.",
        "",
        "The ports read object fields through byte offsets. Keeping those offsets",
        "here, derived from the same headers the C runtime compiles against, is",
        "what stops a C-side layout change from silently missing the mirror",
        "(ARCH-P2-PORT-ABI-AUTOGEN).",
        '"""',
        "",
        "# Runtime ports keep raw pointers in the pointer lane; the frontend reads",
        "# this directive from every module compiled into the runtime archive.",
        "__pcc_runtime_port__ = True",
    ]
    for heading, items in _constant_groups(abi):
        out += ["", f"# --- {heading} ---"]
        for name, value in items:
            if name == "PY_FLAG_GEN_SOURCE":
                out.append("# Source generator objects are iterator data even if their body can park.")
            out.append(f"{name} = {value}")
    out.append("")
    return "\n".join(out)


def render_exports(abi: dict[str, dict[str, int]]) -> str:
    out: list[str] = [
        '"""Generated static exports for pcc-Python runtime ABI constants.',
        "",
        "GENERATED by scripts/gen_port_abi_constants.py from the C runtime",
        "headers. Do not edit by hand.",
        '"""',
        "",
        "PORT_ABI_NATIVE_EXPORTS = {",
        '    "pcc.runtime.py.py_abi_constants": {',
    ]
    for name, value in _constant_items(abi):
        out.append(
            '        "'
            + name
            + '": {"kind": "constant", "value_kind": "int", "value": '
            + str(value)
            + "},"
        )
    out.extend(["    },", "}", ""])
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true",
        help="fail if the committed file differs from a fresh generation",
    )
    args = parser.parse_args()
    abi = read_abi()
    text = render(abi)
    exports_text = render_exports(abi)
    if args.check:
        expected = ((OUTPUT, text), (EXPORTS_OUTPUT, exports_text))
        for path, generated_text in expected:
            if not path.exists():
                print(f"{path} is missing; run scripts/gen_port_abi_constants.py")
                return 1
            current = path.read_text(encoding="utf-8")
            if current != generated_text:
                print(
                    f"{path} is stale relative to the C headers; "
                    "run scripts/gen_port_abi_constants.py"
                )
                return 1
        print(f"{OUTPUT} and {EXPORTS_OUTPUT} match the C headers")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    EXPORTS_OUTPUT.write_text(exports_text, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    print(f"wrote {EXPORTS_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
