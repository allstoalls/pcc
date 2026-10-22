"""Closed-world import ownership policy for the Python frontend.

Keep the classification tables in one module so dependency discovery and
libpython fallback analysis cannot silently drift apart.
"""

from __future__ import annotations


COMPILE_TIME_ONLY_IMPORT_FROMS = {
    "abc": frozenset({"ABC", "abstractmethod"}),
    "dataclasses": frozenset({"dataclass", "field", "replace"}),
}


def dataclasses_field_binding_names(module):
    from pcc.frontends.python.py_ast import Import, ImportFrom
    bindings = []
    for statement in module.body:
        if isinstance(statement, ImportFrom) and statement.module == "dataclasses":
            for imported, alias in statement.names:
                if imported == "field":
                    bindings.append(alias or imported)
        if isinstance(statement, Import):
            for imported, alias in statement.names:
                if imported == "dataclasses":
                    bindings.append((alias or imported) + ".field")
    return tuple(bindings)


def dataclasses_field_runtime_names(module):
    """Return actual imported field bindings used beyond dataclass declarations.

    A field Call consumed by class expansion needs no runtime provider. A
    function default, alias or ordinary value read needs the owned field().
    """
    from pcc.frontends.python.py_ast import Assign, Call, ClassDef, Import, ImportFrom, Name, Type
    from pcc.frontends.python.pipeline_ast_wire import _py_ast_field_names, _py_ast_field_value
    from pcc.frontends.python.pipeline_exports import _class_is_dataclass

    bindings = dataclasses_field_binding_names(module)
    if not bindings:
        return ()
    consumed = []
    pending = list(_py_ast_field_value(module, "body", ()))
    while pending:
        node = pending.pop()
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        if isinstance(node, (str, int, float, bool, Type)) or node is None:
            continue
        if isinstance(node, ClassDef) and _class_is_dataclass(node):
            for statement in node.body:
                if not isinstance(statement, Assign) or statement.annotation is None:
                    continue
                value = statement.value
                if isinstance(value, Call) and isinstance(value.func, Name) and value.func.ident in bindings:
                    consumed.append(value.func)
        if isinstance(node, (Import, ImportFrom)):
            continue
        for field_name in _py_ast_field_names(node):
            if field_name not in ("annotation", "return_ty", "ty", "span"):
                pending.append(_py_ast_field_value(node, field_name, None))
    runtime = []
    pending = list(_py_ast_field_value(module, "body", ()))
    while pending:
        node = pending.pop()
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        if isinstance(node, (str, int, float, bool, Type)) or node is None:
            continue
        if isinstance(node, Name) and node.ident in bindings:
            macro_only = False
            for macro in consumed:
                if node is macro:
                    macro_only = True
                    break
            if not macro_only and node.ident not in runtime:
                runtime.append(node.ident)
        if isinstance(node, (Import, ImportFrom)):
            continue
        for field_name in _py_ast_field_names(node):
            if field_name not in ("annotation", "return_ty", "ty", "span"):
                pending.append(_py_ast_field_value(node, field_name, None))
    return tuple(runtime)

COMPILE_TIME_ONLY_IMPORT_MODULES = frozenset(
    {"__future__", "typing", "click", "abc"}
)

TEST_FACADE_IMPORT_MODULES = ("pytest", "pcc.test_runner")

# Deliberate runtime components resolved from the pcc source/install root even
# when the application lives outside that tree. The literal semantic target is
# shared with stdlib providers; compiler implementation modules stay excluded.
# Gateway/web packages resolve through the package site after their repo split.
PCC_OWNED_COMPONENT_IMPORT_PREFIXES: tuple[str, ...] = (
    "pcc.driver.python_target", "pcc.stdlib._structseq",
)

ANNOTATION_ONLY_IMPORT_MODULES = frozenset(
    {"llvmlite.binding", "llvmlite.ir"}
)
# This candidate set only applies when runtime-name analysis proves a binding
# unused. Live imports obey the same owned/strict policy as every other module.

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
# exception classes belong to their pcc-Python providers in the closed world,
# and so do contextvars' `Context`/`copy_context`: dedicated lowering owns
# `ContextVar` only, so without a walked provider `copy_context()` resolved
# through CPython and took every function that called it -- including
# `asyncio.Task.__init__` -- down to a fail-closed no-libpython stub.
NATIVE_BUILTIN_IMPORTS_WITH_COMPILED_PROVIDER = frozenset(
    # ``contextlib``: builtin dispatch covers ``with`` statement lowering, not
    # ``contextmanager``/``nullcontext`` as values.  ``pcc/frontends/c/codegen/c_codegen``
    # imports both at module scope, so a pcc1 carrying the C frontend emitted
    # a native module import for a module its closure did not contain and
    # died at runtime with "No module named 'contextlib'".
    # functools also exposes real factories and metadata-bearing callable
    # values; recognizing decorator syntax alone cannot own its imports.
    # ``json``: builtin dispatch owns ``loads``/``dumps``, but
    # ``JSONDecodeError`` is a class value.  ``except json.JSONDecodeError``
    # left ``py_cpy_ensure_init`` in the IR, so ``--python-libpython=off``
    # replaced the whole enclosing function with a fail-closed stub, and
    # ``from json import JSONDecodeError`` raised "No module named 'json'".
    # ``pcc/tools/runtime_archive_provenance._load_json_object`` catches that
    # class, so pcc1 could not verify a runtime archive it had just built:
    # ``provenance_valid`` swallows the NotImplementedError and answers False,
    # and every compile then failed with "runtime archive has invalid
    # provenance" -- including the stage1 function smoke.
    # ``os``: intrinsic lowering owns filesystem operations, but ``PathLike``
    # and ``fspath`` are semantic objects in its compiled provider. Excluding
    # it routes those attributes through CPython and stubs callers in strict
    # no-libpython mode, including compiler error reporting.
    # ``shlex``: intrinsic dispatch owns the narrow split call, while quote,
    # join and imported callable values belong to the existing source provider.
    # Worker command construction needs those values in the native closure.
    # ``pickle``: as for json, builtin dispatch owns dumps/loads while
    # ``HIGHEST_PROTOCOL``, ``DEFAULT_PROTOCOL`` and the error classes are
    # values of the compiled provider; ``range(pickle.HIGHEST_PROTOCOL + 1)``
    # stubbed its whole function under --python-libpython=off.
    {"os", "platform", "subprocess", "contextvars", "contextlib", "functools",
     "json", "math", "time", "shlex", "pickle"}
)



def native_builtin_dispatch_owns(module_name: str, attribute: str) -> bool:
    """Whether builtin dispatch lowers ``module.attribute(...)`` although the
    module also has a compiled provider binding. pickle's provider keeps a
    host-CPython fallback for dumps/loads whose flattened format cannot nest
    (``[1, (2, 3.5)]`` came back as ``[1, (2,), 3.5]``); a provider's other
    values, and functools.partial, belong to the provider."""
    return module_name == "pickle" and attribute in ("dumps", "loads")


# A shallow explicit multi-file compile normally admits every directly
# imported pcc-owned provider.  Entries are needed here only when the module is
# also classified as compiler-owned builtin dispatch but still exposes
# semantic objects that require its compiled provider.
REQUIRED_COMPILED_STDLIB_PROVIDERS = frozenset(
    {"os", "platform", "subprocess", "contextvars", "contextlib", "functools",
     "json", "math", "time", "shlex"}
)

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
    "sys": frozenset({"exit", "getrecursionlimit", "stdin", "stdout", "stderr"}),
    "os": frozenset({"path", "name", "sep", "linesep", "altsep", "pathsep"}),
    "time": frozenset(
        {
            "monotonic",
            "perf_counter",
            "time",
            "strftime",
            "monotonic_ns",
            "perf_counter_ns",
            "time_ns",
        }
    ),
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
            "BoundedSemaphore",
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
            "i64",
            "u64",
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
    {"pcc.extern", "pcc.ir", "pcc.ir.compat", "pcc.unsafe"}
)
