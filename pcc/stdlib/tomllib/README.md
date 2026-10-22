# Owned tomllib source

This provider is derived from the installed CPython 3.15.0rc1 `Lib/tomllib`
sources. It retains the general parser, regular expressions, callback behavior,
and `TOMLDecodeError` implementation. It has no package-specific parsing rules.

The source files carry the upstream MIT SPDX identifiers, Taneli Hukkinen's
copyright notice, and the notice that the code was licensed to the Python
Software Foundation under a Contributor Agreement. The MIT grant is in
`LICENSE`.

Upstream source identities before adaptation:

| File | SHA-256 |
| --- | --- |
| `__init__.py` | `71f67036895f4c5acab942618af0cbd3d814451ba61e967f358d0f341a5b8f51` |
| `_parser.py` | `1eca6101d6135d4ba30573eb2212becb4cea39cb3416f48225a1deeecdd94c4d` |
| `_re.py` | `a12359fe294523a72112e434d58452a14c9d050affa2417f9927474e4166bfdd` |
| `_types.py` | `f864c6d9552a929c7032ace654ee05ef26ca75d21b027b801d77e65907138b74` |

Adaptations for pcc:

- Omit the source's Python-before-3.15 `MappingProxyType` compatibility import;
  the pcc semantic target is Python 3.15.
- Store the private escape-replacement table in a `dict`, since the owned
  runtime does not yet provide Python 3.15's `frozendict`. The parser only reads
  this table. This does not preserve the upstream table's private immutability.
- Omit bare `Final` annotations on constants; these markers have no runtime
  effect and pcc's frontend does not accept their unparameterized form.
- Expand the annotation-only aliases `Key`, `Pos`, and `ParseFloat` to
  `tuple[str, ...]`, `int`, and `Callable[[str], Any]`. Imported aliases behind
  `TYPE_CHECKING` currently remain unresolved class shells in pcc; the port
  retains their declared types without changing parser operations.
- Annotate `make_safe_parse_float`'s return as `Any`; its unchanged fast path
  returns the builtin `float`, which pcc currently rejects against
  `Callable[[str], Any]` despite accepting the callback's runtime behavior.

Host differential tests exercise the public parsing and error behavior.
Recursive no-libpython IR emission and native execution have separate gates;
source availability and host tests do not certify native implementation.
