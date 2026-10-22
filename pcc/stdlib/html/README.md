# Owned HTML support

The `html`, `html.entities`, and `html.parser` providers implement HTML escaping,
HTML5 character references, and the ordinary `HTMLParser` streaming callback API.
They are selected by the existing dotted stdlib resolver. There is no foreign
constructor fallback or package-specific parsing path.

The parser state machine, error recovery, numeric-reference tables, and complete
HTML entity tables derive from the installed official CPython 3.15.0rc1 sources.
They retain the Python license in `LICENSE.txt`. The source oracle reports
`3.15.0rc1 (main, Aug 25 2026, 14:02:32) [Clang 22.1.3 ]`.

The native regex engine does not implement the lookbehind used by CPython's
attribute regex. This provider therefore replaces token regexes with character
scans and owns position tracking directly. It preserves real initialization,
reset/feed/close, subclass callbacks, start/end/self-closing tags, duplicate and
boolean attributes, references, comments, declarations, processing instructions,
CDATA, raw-text/RCDATA, scripting mode, and partial-token buffering. Internal
`_markupbase.ParserBase` SGML helpers and private compiled-regex attributes are
not part of this provider's compatibility surface.

Host CPython differential tests live in
`tests/python/test_py_stdlib_xml_unicode_closure.py`; owned import/constructor/IR
controls live in `tests/python/test_recursive_stdlib_import_codegen.py`.
The separate native integration gate executes both a generic recording subclass
and the original imported `pcc.package.acquire._SimpleLinks` under all five GCs.
Host and IR checks alone do not qualify that native behavior.
