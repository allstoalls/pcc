"""Closed-world import ownership policy for the Python frontend.

Keep the classification tables in one module so dependency discovery and
libpython fallback analysis cannot silently drift apart.
"""

from __future__ import annotations


COMPILE_TIME_ONLY_IMPORT_FROMS = {
    "abc": frozenset({"ABC", "abstractmethod"}),
    "dataclasses": frozenset({"dataclass", "field", "replace"}),
}

COMPILE_TIME_ONLY_IMPORT_MODULES = frozenset(
    {"__future__", "typing", "click", "abc"}
)

TEST_FACADE_IMPORT_MODULES = ("pytest", "pcc.test_runner")

# Deliberate runtime components resolved from the pcc source/install root even
# when the application lives outside that tree. The literal semantic target is
# shared with stdlib providers; compiler implementation modules stay excluded.
# Gateway/web packages resolve through the package site after their repo split.
PCC_OWNED_COMPONENT_IMPORT_PREFIXES: tuple[str, ...] = ("pcc.python_target",)

ANNOTATION_ONLY_IMPORT_MODULES = frozenset(
    {"llvmlite.binding", "llvmlite.ir"}
)

NATIVE_BUILTIN_IMPORTS = frozenset(
    {
        "builtins",
        "sys",
        "os",
        "time",
        "string",
        "platform",
        "subprocess",
        "tempfile",
        "shutil",
        "shlex",
        "math",
        "json",
        "re",
        "gc",
        "weakref",
        "copy",
        "functools",
        "pickle",
        "threading",
        "pcc.virtual_thread",
        "pcc",
        "inspect",
        "contextlib",
        "contextvars",
        # textwrap now has a compiled provider and must be walked rather than
        # hidden here. enum remains compiler-owned by class lowering, so its
        # metaclass-heavy stdlib implementation stays outside the closure.
        "enum",
    }
)

# Most builtin-native modules need no compiled provider because dedicated
# lowering owns their values. platform's version functions and subprocess's
# exception classes belong to their pcc-Python providers in the closed world.
NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER = frozenset({"platform", "subprocess"})

# A shallow explicit multi-file compile normally admits every directly
# imported pcc-owned provider.  Entries are needed here only when the module is
# also classified as compiler-owned builtin dispatch but still exposes
# semantic objects that require its compiled provider.
REQUIRED_COMPILED_STDLIB_PROVIDERS = frozenset({"platform", "subprocess"})

NATIVE_IMPORT_FROMS = {
    "builtins": frozenset(
        {
            "bool",
            "bytes",
            "bytearray",
            "complex",
            "dict",
            "float",
            "int",
            "list",
            "memoryview",
            "object",
            "str",
            "tuple",
        }
    ),
    "sys": frozenset({"exit", "stdin", "stdout", "stderr"}),
    "os": frozenset({"path", "sep", "linesep", "altsep"}),
    "time": frozenset({"monotonic", "perf_counter", "time", "strftime"}),
    "functools": frozenset({"partial"}),
    "string": frozenset(
        {
            "ascii_lowercase",
            "ascii_uppercase",
            "ascii_letters",
            "digits",
            "hexdigits",
            "octdigits",
            "punctuation",
            "whitespace",
            "printable",
        }
    ),
    "math": frozenset(
        {
            "floor",
            "ceil",
            "sqrt",
            "trunc",
            "gcd",
            "factorial",
            "isqrt",
            "pow",
            "pi",
            "e",
            "tau",
            "inf",
            "nan",
        }
    ),
    "re": frozenset({"match", "search", "fullmatch"}),
    "gc": frozenset(
        {
            "collect",
            "disable",
            "enable",
            "isenabled",
            "is_tracked",
            "is_finalized",
            "get_count",
            "get_threshold",
            "set_threshold",
            "get_stats",
            "freeze",
            "unfreeze",
            "get_freeze_count",
            "get_objects",
            "get_referents",
            "get_referrers",
        }
    ),
    "weakref": frozenset({"ref"}),
    "threading": frozenset(
        {
            "Thread",
            "Lock",
            "RLock",
            "Event",
            "Condition",
            "Semaphore",
            "current_thread",
            "get_ident",
        }
    ),
    "pcc.virtual_thread": frozenset(
        {
            "OUTCOME_PENDING",
            "OUTCOME_RETURNED",
            "OUTCOME_RAISED",
            "OUTCOME_CANCELLED",
            "RECV_VALUE",
            "RECV_SENDER_CLOSED",
            "RECV_RECEIVER_CLOSED",
            "SELECT_LEFT",
            "SELECT_RIGHT",
            "spawn",
            "continuation",
            "completed",
            "continuation_factory",
            "call",
            "join",
            "cancel",
            "mpsc",
            "oneshot",
            "sender_clone",
            "send",
            "recv",
            "close_sender",
            "close_receiver",
            "select2",
            "run",
            "run_until_idle",
            "carrier_pool_start",
            "carrier_pool_stop",
            "io_backend",
            "current",
            "yield_now",
            "sleep_current",
            "block_current_on_fd",
            "readable",
            "writable",
            "tcp_listen",
            "tcp_accept",
            "tcp_connect",
            "tcp_recv",
            "tcp_send_all",
            "tcp_close",
            "result",
            "exception",
            "outcome",
            "state",
            "sleep",
            "block_on_fd",
        }
    ),
    "contextlib": frozenset({"contextmanager"}),
    "contextvars": frozenset({"ContextVar"}),
    "pcc": frozenset(
        {
            "valueclass",
            "i64_buffer",
            "guarded_i64_dot",
            "guarded_loop_counter",
        }
    ),
    "enum": frozenset({"Enum", "IntEnum", "auto"}),
    "typing": frozenset(
        {
            "Generic",
            "Protocol",
            "TypeVar",
            "runtime_checkable",
            "get_origin",
            "get_args",
            "Optional",
        }
    ),
}

SCAFFOLD_IMPORT_MODULES = frozenset(
    {"pcc.extern", "pcc.llvm_capi", "pcc.llvm_capi.compat", "pcc.unsafe"}
)
