"""Builtin functions as first-class values, written in Python.

A builtin named at value position (``f = len``, ``key=len``, ``builtins.len``,
``from builtins import len``) needs a function object. Each supported builtin
has a Python wrapper below: it parses its arguments exactly as CPython's C
implementation does (Argument Clinic messages included) and then calls the
builtin directly, so the value shares the direct-call lowering's semantics.

``inject_builtin_function_values`` adds the wrappers a module references to
that module's AST. Code generation publishes a wrapper through the runtime's
process-wide builtin table, which flags it as a builtin function and keeps
one object per name: ``len is len`` across every compiled module.
"""

from __future__ import annotations

from pcc.frontends.python import py_parse as pp
from pcc.frontends.python.pipeline_ast_wire import _py_ast_field_names, _py_ast_field_value, _py_ast_node_replace
from pcc.frontends.python.py_ast import (
    Assign, Attr, BoolLit, Call, FuncDef, ImportFrom, ListExpr, Module, Name,
    Subscript, TupleExpr,
)

BUILTIN_VALUE_PREFIX = "__pcc_builtin_value_"
BUILTIN_HELPER_PREFIX = "__pcc_builtin_"
BUILTIN_SOURCE_FILE = "<pcc-builtins>"

_HELPERS = {
    "exactly_one": \
'''def __pcc_builtin_exactly_one(name, args, kwargs):
    if kwargs:
        raise TypeError(name + "() takes no keyword arguments")
    if len(args) != 1:
        raise TypeError(name + "() takes exactly one argument (" + str(len(args)) + " given)")
''',
    "positional": \
'''def __pcc_builtin_positional(name, args, kwargs, low, high):
    if kwargs:
        raise TypeError(name + "() takes no keyword arguments")
    count = len(args)
    if low == high:
        if count != low:
            raise TypeError(name + " expected " + str(low) + " arguments, got " + str(count))
    elif count < low:
        noun = " argument, got " if low == 1 else " arguments, got "
        raise TypeError(name + " expected at least " + str(low) + noun + str(count))
    elif count > high:
        noun = " argument, got " if high == 1 else " arguments, got "
        raise TypeError(name + " expected at most " + str(high) + noun + str(count))
''',
    "clinic": \
'''def __pcc_builtin_clinic(name, args, kwargs, names, required, keyword_from):
    count = len(args)
    if count > len(names):
        raise TypeError(name + "() takes at most " + str(len(names)) + " arguments (" + str(count) + " given)")
    values = list(args)
    index = count
    while index < len(names):
        parameter = names[index]
        if index >= keyword_from and parameter in kwargs:
            values.append(kwargs[parameter])
        elif index < required:
            raise TypeError(name + "() missing required argument '" + parameter + "' (pos " + str(index + 1) + ")")
        else:
            values.append(None)
        index += 1
    for keyword in kwargs:
        position = -1
        index = keyword_from
        while index < len(names):
            if names[index] == keyword:
                position = index
            index += 1
        if position < 0:
            raise TypeError(name + "() got an unexpected keyword argument '" + keyword + "'")
        if position < count:
            raise TypeError("argument for " + name + "() given by name ('" + keyword + "') and position (" + str(position + 1) + ")")
    return values
''',
    "min_max": \
'''def __pcc_builtin_min_max(name, args, kwargs, want_max):
    key = None
    has_default = False
    default = None
    for keyword in kwargs:
        if keyword == "key":
            key = kwargs[keyword]
        elif keyword == "default":
            has_default = True
            default = kwargs[keyword]
        else:
            raise TypeError(name + "() got an unexpected keyword argument '" + keyword + "'")
    count = len(args)
    if count == 0:
        raise TypeError(name + " expected at least 1 argument, got 0")
    if count == 1:
        items = args[0]
    elif has_default:
        raise TypeError("Cannot specify a default for " + name + "() with multiple positional arguments")
    else:
        items = args
    found = False
    best = None
    best_key = None
    for item in items:
        item_key = item if key is None else key(item)
        if not found:
            found = True
            best = item
            best_key = item_key
        elif want_max:
            if item_key > best_key:
                best = item
                best_key = item_key
        elif item_key < best_key:
            best = item
            best_key = item_key
    if not found:
        if has_default:
            return default
        raise ValueError(name + "() iterable argument is empty")
    return best
''',
}


def _exactly_one(name):
    return (("exactly_one",),
            "def " + BUILTIN_VALUE_PREFIX + name + "(*args, **kwargs):\n"
            "    __pcc_builtin_exactly_one(\"" + name + "\", args, kwargs)\n"
            "    return " + name + "(args[0])\n")


def _positional(name, low, high, bodies):
    lines = ["def " + BUILTIN_VALUE_PREFIX + name + "(*args, **kwargs):\n",
             "    __pcc_builtin_positional(\"" + name + "\", args, kwargs, "
             + str(low) + ", " + str(high) + ")\n"]
    for count in range(low, high + 1):
        call = bodies[count]
        if count == high:
            lines.append("    " + call + "\n")
        else:
            lines.append("    if len(args) == " + str(count) + ":\n")
            lines.append("        " + call + "\n")
    return (("positional",), "".join(lines))


_SOURCES = {
    "abs": _exactly_one("abs"),
    "all": _exactly_one("all"),
    "any": _exactly_one("any"),
    "ascii": _exactly_one("ascii"),
    "bin": _exactly_one("bin"),
    "callable": _exactly_one("callable"),
    "chr": _exactly_one("chr"),
    "hash": _exactly_one("hash"),
    "hex": _exactly_one("hex"),
    "id": _exactly_one("id"),
    "len": _exactly_one("len"),
    "oct": _exactly_one("oct"),
    "ord": _exactly_one("ord"),
    "repr": _exactly_one("repr"),
    "delattr": _positional("delattr", 2, 2, {2: "delattr(args[0], args[1])"}),
    "divmod": _positional("divmod", 2, 2, {2: "return divmod(args[0], args[1])"}),
    "format": _positional("format", 1, 2, {1: "return format(args[0])",
                                            2: "return format(args[0], args[1])"}),
    "getattr": _positional("getattr", 2, 3, {2: "return getattr(args[0], args[1])",
                                              3: "return getattr(args[0], args[1], args[2])"}),
    "hasattr": _positional("hasattr", 2, 2, {2: "return hasattr(args[0], args[1])"}),
    "input": _positional("input", 0, 1, {0: "return input()", 1: "return input(args[0])"}),
    "isinstance": _positional("isinstance", 2, 2, {2: "return isinstance(args[0], args[1])"}),
    "issubclass": _positional("issubclass", 2, 2, {2: "return issubclass(args[0], args[1])"}),
    "iter": _positional("iter", 1, 2, {1: "return iter(args[0])", 2: "return iter(args[0], args[1])"}),
    "next": _positional("next", 1, 2, {1: "return next(args[0])", 2: "return next(args[0], args[1])"}),
    "setattr": _positional("setattr", 3, 3, {3: "setattr(args[0], args[1], args[2])"}),
    "max": (("min_max",),
            "def " + BUILTIN_VALUE_PREFIX + "max(*args, **kwargs):\n"
            "    return __pcc_builtin_min_max(\"max\", args, kwargs, True)\n"),
    "min": (("min_max",),
            "def " + BUILTIN_VALUE_PREFIX + "min(*args, **kwargs):\n"
            "    return __pcc_builtin_min_max(\"min\", args, kwargs, False)\n"),
    "pow": (("clinic",),
            "def " + BUILTIN_VALUE_PREFIX + "pow(*args, **kwargs):\n"
            "    values = __pcc_builtin_clinic(\"pow\", args, kwargs, (\"base\", \"exp\", \"mod\"), 2, 0)\n"
            "    return pow(values[0], values[1], values[2])\n"),
    "round": (("clinic",),
              "def " + BUILTIN_VALUE_PREFIX + "round(*args, **kwargs):\n"
              "    values = __pcc_builtin_clinic(\"round\", args, kwargs, (\"number\", \"ndigits\"), 1, 0)\n"
              "    if values[1] is None:\n"
              "        return round(values[0])\n"
              "    return round(values[0], values[1])\n"),
    "sum": (("clinic",),
            "def " + BUILTIN_VALUE_PREFIX + "sum(*args, **kwargs):\n"
            "    if len(args) == 0:\n"
            "        raise TypeError(\"sum() takes at least 1 positional argument (0 given)\")\n"
            "    values = __pcc_builtin_clinic(\"sum\", args, kwargs, (\"iterable\", \"start\"), 1, 1)\n"
            "    if len(args) < 2 and \"start\" not in kwargs:\n"
            "        return sum(values[0])\n"
            "    return sum(values[0], values[1])\n"),
    "print": ((),
              "def " + BUILTIN_VALUE_PREFIX + "print(*args, **kwargs):\n"
              "    sep = None\n"
              "    end = None\n"
              "    file = None\n"
              "    flush = False\n"
              "    for keyword in kwargs:\n"
              "        if keyword == \"sep\":\n"
              "            sep = kwargs[keyword]\n"
              "        elif keyword == \"end\":\n"
              "            end = kwargs[keyword]\n"
              "        elif keyword == \"file\":\n"
              "            file = kwargs[keyword]\n"
              "        elif keyword == \"flush\":\n"
              "            flush = kwargs[keyword]\n"
              "        else:\n"
              "            raise TypeError(\"print() got an unexpected keyword argument '\" + keyword + \"'\")\n"
              "    print(*args, sep=sep, end=end, file=file, flush=flush)\n"),
    "sorted": ((),
               "def " + BUILTIN_VALUE_PREFIX + "sorted(*args, **kwargs):\n"
               "    if len(args) != 1:\n"
               "        raise TypeError(\"sorted expected 1 argument, got \" + str(len(args)))\n"
               "    result = list(args[0])\n"
               "    key = None\n"
               "    reverse = False\n"
               "    for keyword in kwargs:\n"
               "        if keyword == \"key\":\n"
               "            key = kwargs[keyword]\n"
               "        elif keyword == \"reverse\":\n"
               "            reverse = kwargs[keyword]\n"
               "        else:\n"
               "            raise TypeError(\"sort() got an unexpected keyword argument '\" + keyword + \"'\")\n"
               "    result.sort(key=key, reverse=reverse)\n"
               "    return result\n"),
}


def builtin_function_value_names() -> frozenset:
    """Builtin functions that have a first-class value wrapper."""
    return frozenset(_SOURCES)


def builtin_function_value_name_exists(name: str) -> bool:
    """Query the live string-keyed wrapper registry without a snapshot."""
    if type(name) is str and type(_SOURCES) is dict:
        return name in _SOURCES
    return name in builtin_function_value_names()


# Keywords a builtin's direct call lowering parses itself. Any other keyword,
# and a * or ** argument (print parses its own *args), takes the wrapper.
_DIRECT_CALL_KEYWORDS = {
    "print": ("sep", "end", "file", "flush"),
    "sorted": ("key", "reverse"),
}
_UNPACK_MARKERS = ("*", "__starred__", "**")


def builtin_call_needs_wrapper(name: str, call: Call) -> bool:
    """Whether a call of builtin ``name`` runs its Python wrapper.

    The wrapper parses arguments as CPython's C implementation does; it
    takes every shape the builtin's direct lowering does not parse.
    """
    if name not in _SOURCES:
        return False
    for argument in call.args:
        if (isinstance(argument, Call) and isinstance(argument.func, Name)
                and argument.func.ident in _UNPACK_MARKERS):
            if name != "print" or argument.func.ident == "**":
                return True
    allowed = _DIRECT_CALL_KEYWORDS.get(name, ())
    keywords = [keyword for keyword, _value in call.kwargs]
    for keyword, value in call.kwargs:
        if keyword not in allowed:
            return True
        # print drops a bool-literal flush (it always flushes); a computed
        # flush is evaluated by the wrapper, or by the file-stream path.
        if (name == "print" and keyword == "flush" and "file" not in keywords
                and not isinstance(value, BoolLit)):
            return True
    return False


def _referenced_assignment_target_values(target, found: set) -> None:
    """Collect reads needed to assign, not the names being bound."""
    if isinstance(target, Name):
        return
    if isinstance(target, (TupleExpr, ListExpr)):
        for item in target.elems:
            _referenced_assignment_target_values(item, found)
        return
    if isinstance(target, Attr):
        _referenced_builtin_values(target.obj, found)
        return
    if isinstance(target, Subscript):
        _referenced_builtin_values(target.obj, found)
        _referenced_builtin_values(target.idx, found)
        return
    if isinstance(target, Call) and isinstance(target.func, Name):
        if target.func.ident in ("*", "__starred__") and len(target.args) == 1:
            _referenced_assignment_target_values(target.args[0], found)
            return
        if target.func.ident in ("_walrus", "__walrus__") and len(target.args) == 2:
            # A nested walrus in target position encodes a chained assignment:
            # both operands are destinations for the enclosing value.
            for item in target.args:
                _referenced_assignment_target_values(item, found)
            return
    # Keep unknown target shapes conservative rather than losing a read.
    _referenced_builtin_values(target, found)


def _referenced_builtin_values(node, found: set) -> None:
    """Collect builtin names at value position: every Name except a call's
    own callee, ``builtins.<name>`` attributes and ``from builtins import``;
    a call shape only the wrapper parses also needs the wrapper."""
    if isinstance(node, Name):
        if node.ident in _SOURCES:
            found.add(node.ident)
        return
    if isinstance(node, Attr):
        obj = node.obj
        if isinstance(obj, Name) and obj.ident == "builtins" and node.name in _SOURCES:
            found.add(node.name)
    if isinstance(node, ImportFrom):
        if node.module == "builtins":
            for attr_name, _as_name in node.names:
                if attr_name in _SOURCES:
                    found.add(attr_name)
        return
    if isinstance(node, Assign):
        for target in node.targets:
            _referenced_assignment_target_values(target, found)
        _referenced_builtin_values(node.value, found)
        _referenced_builtin_values(node.annotation, found)
        return
    if isinstance(node, Call):
        if (
            isinstance(node.func, Name)
            and node.func.ident in ("_walrus", "__walrus__")
            and len(node.args) == 2
        ):
            _referenced_assignment_target_values(node.args[0], found)
            _referenced_builtin_values(node.args[1], found)
            for _keyword, value in node.kwargs:
                _referenced_builtin_values(value, found)
            return
        if not isinstance(node.func, Name):
            _referenced_builtin_values(node.func, found)
        elif builtin_call_needs_wrapper(node.func.ident, node):
            found.add(node.func.ident)
        for argument in node.args:
            _referenced_builtin_values(argument, found)
        for _keyword, value in node.kwargs:
            _referenced_builtin_values(value, found)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _referenced_builtin_values(item, found)
        return
    # Primitive leaves have no fields. (None must be skipped explicitly: its
    # class name matches the ``NoneType`` type node's closed-world kind.)
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    for field in _py_ast_field_names(node):
        if field in ("span", "ty"):
            continue
        _referenced_builtin_values(_py_ast_field_value(node, field, None), found)


_PARSED: dict = {}


def _parse_function(name: str, source: str) -> FuncDef:
    cached = _PARSED.get(name)
    if cached is not None:
        return cached
    from pcc.frontends.python.py_lift import lift_module

    module = lift_module(pp.parse(source, filename=BUILTIN_SOURCE_FILE), BUILTIN_SOURCE_FILE, "builtins")
    function = module.body[0]
    if isinstance(function, FuncDef):
        _PARSED[name] = function
        return function
    raise TypeError("builtin function source must start with a function definition")


_RAW_MODULE_DIRECTIVES = ("__pcc_freestanding__", "__pcc_runtime_port__")


def _declares_raw_module(module: Module) -> bool:
    """Freestanding and runtime-port modules have no Python builtins."""
    for stmt in module.body:
        if (
            isinstance(stmt, Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], Name)
            and stmt.targets[0].ident in _RAW_MODULE_DIRECTIVES
        ):
            return True
    return False


def inject_builtin_function_values(module: Module) -> Module:
    """Add the wrapper (and helper) definitions ``module`` references."""
    if _declares_raw_module(module):
        return module
    found: set = set()
    _referenced_builtin_values(module.body, found)
    if not found:
        return module
    helpers: list = []
    wrappers: list = []
    for name in sorted(found):
        depends, source = _SOURCES[name]
        for helper in depends:
            if helper not in helpers:
                helpers.append(helper)
        wrappers.append(_parse_function(BUILTIN_VALUE_PREFIX + name, source))
    definitions = [_parse_function(BUILTIN_HELPER_PREFIX + helper, _HELPERS[helper]) for helper in helpers]
    return _py_ast_node_replace(module, {"body": tuple(definitions) + tuple(wrappers) + tuple(module.body)})
