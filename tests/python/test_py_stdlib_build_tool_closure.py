"""Differential tests for the pcc/stdlib modules added for the build-tool
import closure (STDLIB-P1-BUILD-TOOL-CLOSURE).

Every assertion compares the pcc port against CPython's own module on the same
input. A module that merely imports is not support; the contract is behavior.

Note on ``fnmatch.translate``: the emitted regex TEXT is deliberately not
compared. CPython's translation changed shape across versions (atomic groups
for ``*`` runs); what must agree is what the pattern matches.
"""
from __future__ import annotations

import codecs as host_codecs
import configparser as host_configparser
import difflib as host_difflib
import errno as host_errno
import filecmp as host_filecmp
import fnmatch as host_fnmatch
import glob as host_glob
import gettext as host_gettext
import io
import locale as host_locale
import os
import netrc as host_netrc
import ntpath as host_ntpath
import posixpath as host_posixpath
import pprint as host_pprint
import pwd as host_pwd
import runpy as host_runpy
import signal as host_signal
import stat as host_stat
import sysconfig as host_sysconfig
import textwrap as host_textwrap
import uuid as host_uuid

import pytest

from pcc.stdlib import codecs as port_codecs
from pcc.stdlib import configparser as port_configparser
from pcc.stdlib import difflib as port_difflib
from pcc.stdlib import errno as port_errno
from pcc.stdlib import filecmp as port_filecmp
from pcc.stdlib import fnmatch as port_fnmatch
from pcc.stdlib import glob as port_glob
from pcc.stdlib import gettext as port_gettext
from pcc.stdlib import locale as port_locale
from pcc.stdlib import netrc as port_netrc
from pcc.stdlib import ntpath as port_ntpath
from pcc.stdlib import posixpath as port_posixpath
from pcc.stdlib import pprint as port_pprint
from pcc.stdlib import pwd as port_pwd
from pcc.stdlib import runpy as port_runpy
from pcc.stdlib import signal as port_signal
from pcc.stdlib import stat as port_stat
from pcc.stdlib import sysconfig as port_sysconfig
from pcc.stdlib import textwrap as port_textwrap
from pcc.stdlib import uuid as port_uuid


MEASURED_BUILD_TOOL_GAP = frozenset(
    {
        "bz2",
        "cProfile",
        "codecs",
        "compileall",
        "configparser",
        "difflib",
        "errno",
        "filecmp",
        "fnmatch",
        "gettext",
        "glob",
        "gzip",
        "http",
        "importlib",
        "locale",
        "lzma",
        "msvcrt",
        "netrc",
        "ntpath",
        "posixpath",
        "pprint",
        "pwd",
        "runpy",
        "signal",
        "stat",
        "sysconfig",
        "tarfile",
        "textwrap",
        "unicodedata",
        "unittest",
        "uuid",
        "xml",
        "zipapp",
        "zipfile",
    }
)


ERRNO_NAMES = [
    "EPERM", "ENOENT", "ESRCH", "EINTR", "EIO", "ENXIO", "E2BIG", "ENOEXEC",
    "EBADF", "ECHILD", "ENOMEM", "EACCES", "EFAULT", "EBUSY", "EEXIST",
    "EXDEV", "ENODEV", "ENOTDIR", "EISDIR", "EINVAL", "ENFILE", "EMFILE",
    "ENOTTY", "ETXTBSY", "EFBIG", "ENOSPC", "ESPIPE", "EROFS", "EMLINK",
    "EPIPE", "EDOM", "ERANGE",
]


@pytest.mark.parametrize("name", ERRNO_NAMES)
def test_errno_constants_match_cpython(name):
    assert getattr(port_errno, name) == getattr(host_errno, name)


def test_errno_platform_swapped_codes_match_cpython():
    """EAGAIN and EDEADLK are swapped between Linux (11/35) and Darwin (35/11).

    A single hardcoded table silently produced the wrong EAGAIN on Darwin.
    """
    assert port_errno.EAGAIN == host_errno.EAGAIN
    assert port_errno.EDEADLK == host_errno.EDEADLK
    assert port_errno.EWOULDBLOCK == host_errno.EWOULDBLOCK
    assert port_errno.errorcode[port_errno.EAGAIN] == "EAGAIN"


STAT_MODES = [0o040755, 0o100644, 0o120777, 0o010600, 0o060660, 0o140777, 0o020600]


@pytest.mark.parametrize("mode", STAT_MODES)
@pytest.mark.parametrize(
    "fn",
    ["S_ISDIR", "S_ISREG", "S_ISLNK", "S_ISFIFO", "S_ISCHR", "S_ISBLK",
     "S_ISSOCK", "S_IMODE", "S_IFMT"],
)
def test_stat_decoding_matches_cpython(fn, mode):
    assert getattr(port_stat, fn)(mode) == getattr(host_stat, fn)(mode)


FNMATCH_CASES = [
    ("a.txt", "*.txt"), ("a.txt", "*.py"), ("abc", "a?c"), ("abc", "a[bc]c"),
    ("abc", "a[!b]c"), ("a-c", "a[a-c]c"), ("x", "[]]"), ("]", "[]]"),
    ("a", "[!]"), ("foo.tar.gz", "*.gz"), ("a", "*"), ("", "*"),
    ("a/b", "a/b"), ("a", "[a-"), ("a", "**"), ("ab", "a**b"),
    ("a|b", "a[|]b"), ("-", "[-]"), ("a", "[a-c-e]"), ("e", "[a-c-e]"),
]


@pytest.mark.parametrize("name,pattern", FNMATCH_CASES)
def test_fnmatch_matches_cpython(name, pattern):
    assert port_fnmatch.fnmatch(name, pattern) == host_fnmatch.fnmatch(name, pattern)
    assert port_fnmatch.fnmatchcase(name, pattern) == host_fnmatch.fnmatchcase(
        name, pattern
    )


def test_fnmatch_filter_matches_cpython():
    names = ["a.py", "b.txt", "c.py", ".d.py"]
    assert port_fnmatch.filter(names, "*.py") == host_fnmatch.filter(names, "*.py")


DEDENT_CASES = [
    "    a\n    b\n", "  a\n    b\n", "\ta\n\tb\n", "a\n b\n", "",
    "   \n  x\n", "  a\n\n  b\n",
]


@pytest.mark.parametrize("text", DEDENT_CASES)
def test_textwrap_dedent_matches_cpython(text):
    assert port_textwrap.dedent(text) == host_textwrap.dedent(text)


def test_textwrap_indent_matches_cpython():
    text = "a\nb\n\nc\n"
    assert port_textwrap.indent(text, ">> ") == host_textwrap.indent(text, ">> ")


@pytest.mark.parametrize("width", [5, 10, 20, 70])
def test_textwrap_wrap_and_fill_match_cpython(width):
    text = "the quick brown fox jumps over the lazy dog"
    assert port_textwrap.wrap(text, width) == host_textwrap.wrap(text, width)
    assert port_textwrap.fill(text, width) == host_textwrap.fill(text, width)


def test_textwrap_shorten_matches_cpython():
    assert port_textwrap.shorten("hello  world  again", 12) == host_textwrap.shorten(
        "hello  world  again", 12
    )


PATHS = ["/a/b/c", "a/b", "/", "", "a/", "/a//b", "a.txt", "/x/y.tar.gz", "//", "///a"]


@pytest.mark.parametrize("path", PATHS)
def test_posixpath_split_family_matches_cpython(path):
    assert port_posixpath.split(path) == host_posixpath.split(path)
    assert port_posixpath.basename(path) == host_posixpath.basename(path)
    assert port_posixpath.dirname(path) == host_posixpath.dirname(path)
    assert port_posixpath.splitext(path) == host_posixpath.splitext(path)
    assert port_posixpath.isabs(path) == host_posixpath.isabs(path)
    assert port_posixpath.normcase(path) == host_posixpath.normcase(path)


def test_posixpath_dirname_strips_duplicate_slashes():
    """Regression: pcc's os.path.dirname returned "/a/" for "/a//b"."""
    assert port_posixpath.dirname("/a//b") == "/a"
    assert port_posixpath.dirname("//") == "//"


def test_posixpath_constants_match_cpython():
    assert port_posixpath.sep == host_posixpath.sep
    assert port_posixpath.pathsep == host_posixpath.pathsep
    assert port_posixpath.curdir == host_posixpath.curdir
    assert port_posixpath.pardir == host_posixpath.pardir


def test_ntpath_constants_used_by_cross_platform_meson_match_cpython():
    for name in ("sep", "altsep", "extsep", "pathsep", "curdir", "pardir"):
        assert getattr(port_ntpath, name) == getattr(host_ntpath, name)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\build\meson.py",
        r"C:relative\meson.py",
        r"\\server\share\meson.py",
        r"\rooted\meson.py",
        "plain.py",
        "//server/share/meson.py",
    ],
)
def test_ntpath_split_family_matches_cpython(path):
    assert port_ntpath.splitdrive(path) == host_ntpath.splitdrive(path)
    assert port_ntpath.split(path) == host_ntpath.split(path)
    assert port_ntpath.dirname(path) == host_ntpath.dirname(path)
    assert port_ntpath.basename(path) == host_ntpath.basename(path)
    assert port_ntpath.splitext(path) == host_ntpath.splitext(path)
    assert port_ntpath.isabs(path) == host_ntpath.isabs(path)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\build\.\meson\..\out",
        r"C:build\..\out",
        r"\\server\share\one\..\two",
        r"one/two/../three",
        "",
    ],
)
def test_ntpath_normpath_matches_cpython(path):
    assert port_ntpath.normpath(path) == host_ntpath.normpath(path)


@pytest.mark.parametrize(
    "base,parts",
    [
        (r"C:\build", ("meson", "out")),
        (r"C:\build", (r"\rooted", "out")),
        (r"C:\build", (r"D:relative",)),
        (r"\\server\share", ("meson", "out")),
    ],
)
def test_ntpath_join_matches_cpython(base, parts):
    assert port_ntpath.join(base, *parts) == host_ntpath.join(base, *parts)


@pytest.fixture
def glob_tree(tmp_path, monkeypatch):
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    for rel in ["a.py", "b.txt", ".hidden.py", "sub/c.py", "sub/deep/d.py"]:
        (tmp_path / rel).write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "pattern", ["*.py", "*.txt", "*", "sub/*.py", "sub/*", "./*.py", ".*"]
)
def test_glob_matches_cpython(glob_tree, pattern):
    assert sorted(port_glob.glob(pattern)) == sorted(host_glob.glob(pattern))


def test_glob_recursive_matches_cpython(glob_tree):
    assert sorted(port_glob.glob("**/*.py", recursive=True)) == sorted(
        host_glob.glob("**/*.py", recursive=True)
    )


def test_glob_escape_and_has_magic_match_cpython():
    assert port_glob.escape("a*b?c[d") == host_glob.escape("a*b?c[d")
    assert port_glob.has_magic("a*") == host_glob.has_magic("a*")
    assert port_glob.has_magic("plain") == host_glob.has_magic("plain")


UNICODE_ESCAPE_INPUTS = [
    br"plain",
    br"line\nnext",
    br"\x41",
    br"\u00e9",
    br"\U0001f642",
    br"\101",
    br"\\",
    br"\t",
    br"\N{SNOWMAN}",
    br"\N{LATIN SMALL LETTER A}",
]


@pytest.mark.parametrize("payload", UNICODE_ESCAPE_INPUTS)
def test_codecs_unicode_escape_decode_matches_cpython(payload):
    assert port_codecs.decode(payload, "unicode_escape") == host_codecs.decode(
        payload, "unicode_escape"
    )


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        "line\nnext",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        "\N{SLIGHTLY SMILING FACE}",
        "a\\b",
        "\t",
    ],
)
def test_codecs_unicode_escape_encode_matches_cpython(text):
    assert port_codecs.encode(text, "unicode_escape") == host_codecs.encode(
        text, "unicode_escape"
    )


def test_codecs_bom_constants_match_cpython():
    for name in [
        "BOM_UTF8",
        "BOM_UTF16_LE",
        "BOM_UTF16_BE",
        "BOM_UTF32_LE",
        "BOM_UTF32_BE",
        "BOM_UTF16",
        "BOM_UTF32",
        "BOM_LE",
        "BOM_BE",
        "BOM",
    ]:
        assert getattr(port_codecs, name) == getattr(host_codecs, name)


def test_codecs_utf8_round_trip_matches_cpython():
    text = "caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{SNOWMAN}"
    encoded = port_codecs.encode(text, "utf_8")
    assert encoded == host_codecs.encode(text, "utf_8")
    assert port_codecs.decode(encoded, "utf-8") == host_codecs.decode(
        encoded, "utf-8"
    )


def _normalized_encoding(name):
    return name.lower().replace("-", "").replace("_", "")


def test_locale_preferred_encoding_matches_cpython_utf8_contract():
    # pcc strings and the supported native build-tool hosts are UTF-8.  Compare
    # the semantic codec name because CPython may spell the alias as UTF-8 or
    # utf_8 depending on platform configuration.
    assert _normalized_encoding(port_locale.getpreferredencoding(False)) == (
        _normalized_encoding(host_locale.getpreferredencoding(False))
    )
    assert _normalized_encoding(port_locale.getpreferredencoding()) == (
        _normalized_encoding(host_locale.getpreferredencoding())
    )


PPRINT_CASES = [
    ({"b": 2, "a": 1}, {}),
    ({"b": [1, 2], "a": ("x",)}, {}),
    ([1, 2, 3], {"width": 5}),
    ([0, 1, 2, 3, 4, 5], {"width": 12, "compact": True}),
    ({"z": 0, "a": 1}, {"sort_dicts": False}),
    ([[1, 2]], {"depth": 1}),
    ("alpha beta gamma delta", {"width": 12}),
]


@pytest.mark.parametrize("value,kwargs", PPRINT_CASES)
def test_pprint_pformat_matches_cpython(value, kwargs):
    assert port_pprint.pformat(value, **kwargs) == host_pprint.pformat(
        value, **kwargs
    )


def test_pprint_stream_output_matches_cpython():
    value = {"beta": [1, 2], "alpha": "x"}
    port_stream = io.StringIO()
    host_stream = io.StringIO()
    assert port_pprint.pprint(value, stream=port_stream) is None
    assert host_pprint.pprint(value, stream=host_stream) is None
    assert port_stream.getvalue() == host_stream.getvalue()


def test_pprint_recursive_list_saferepr_matches_cpython():
    value = []
    value.append(value)
    assert port_pprint.saferepr(value) == host_pprint.saferepr(value)


def test_filecmp_full_content_comparison_matches_cpython(tmp_path):
    left = tmp_path / "left.bin"
    same = tmp_path / "same.bin"
    different = tmp_path / "different.bin"
    payload = b"a" * 9000 + b"tail"
    left.write_bytes(payload)
    same.write_bytes(payload)
    different.write_bytes(b"a" * 9000 + b"fail")

    assert port_filecmp.cmp(left, same, shallow=False) == host_filecmp.cmp(
        left, same, shallow=False
    )
    assert port_filecmp.cmp(left, different, shallow=False) == host_filecmp.cmp(
        left, different, shallow=False
    )
    assert port_filecmp.clear_cache() == host_filecmp.clear_cache()


def test_filecmp_shallow_mode_fails_closed_without_native_stat(tmp_path):
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.write_text("same", encoding="utf-8")
    right.write_text("same", encoding="utf-8")
    with pytest.raises(NotImplementedError, match="native stat metadata"):
        port_filecmp.cmp(left, right)


SEQUENCE_MATCHER_CASES = [
    ("abxcd", "abcd", True),
    ("private Thread", "private volatile Thread", True),
    (["a", "b", "c"], ["a", "x", "c", "d"], True),
    ("", "", True),
    ("a" * 205 + "b", "a" * 205 + "c", True),
    ("a" * 205 + "b", "a" * 205 + "c", False),
]


@pytest.mark.parametrize("a,b,autojunk", SEQUENCE_MATCHER_CASES)
def test_difflib_sequence_matcher_matches_cpython(a, b, autojunk):
    port_matcher = port_difflib.SequenceMatcher(None, a, b, autojunk=autojunk)
    host_matcher = host_difflib.SequenceMatcher(None, a, b, autojunk=autojunk)
    assert tuple(port_matcher.find_longest_match()) == tuple(
        host_matcher.find_longest_match()
    )
    assert [tuple(block) for block in port_matcher.get_matching_blocks()] == [
        tuple(block) for block in host_matcher.get_matching_blocks()
    ]
    assert port_matcher.get_opcodes() == host_matcher.get_opcodes()
    assert port_matcher.ratio() == host_matcher.ratio()
    assert port_matcher.quick_ratio() == host_matcher.quick_ratio()
    assert port_matcher.real_quick_ratio() == host_matcher.real_quick_ratio()


def test_difflib_close_matches_matches_cpython():
    possibilities = ["ape", "apple", "peach", "puppy"]
    assert port_difflib.get_close_matches("appel", possibilities) == (
        host_difflib.get_close_matches("appel", possibilities)
    )


@pytest.mark.parametrize(
    "before,after",
    [
        (["same\n", "old\n"], ["same\n", "new\n"]),
        (
            ["one\n", "two\n", "three\n"],
            ["ore\n", "tree\n", "emu\n"],
        ),
        (["alpha\n", "beta\n"], ["zero\n", "alpha\n", "beta\n"]),
    ],
)
def test_difflib_differ_ndiff_and_restore_match_cpython(before, after):
    port_delta = port_difflib.Differ().compare(before, after)
    host_delta = list(host_difflib.Differ().compare(before, after))
    assert port_delta == host_delta
    assert port_difflib.ndiff(before, after) == list(
        host_difflib.ndiff(before, after)
    )
    for which in (1, 2):
        assert port_difflib.restore(port_delta, which) == list(
            host_difflib.restore(host_delta, which)
        )


@pytest.mark.parametrize("line", ["\n", "  #  \n", "hello\n", "##\n"])
def test_difflib_line_junk_matches_cpython(line):
    assert port_difflib.IS_LINE_JUNK(line) == host_difflib.IS_LINE_JUNK(line)


@pytest.mark.parametrize("character", [" ", "\t", "\n", "x"])
def test_difflib_character_junk_matches_cpython(character):
    assert port_difflib.IS_CHARACTER_JUNK(character) == (
        host_difflib.IS_CHARACTER_JUNK(character)
    )


def test_netrc_explicit_file_matches_cpython(tmp_path):
    path = tmp_path / "credentials.netrc"
    path.write_text(
        "machine example.com\n"
        '  login "user name"\n'
        "  account build\n"
        "  password pa\\ ss\n"
        "default login guest password fallback\n"
        "macdef init\n"
        "echo one\n"
        "echo two\n"
        "\n",
        encoding="utf-8",
    )
    port_credentials = port_netrc.netrc(path)
    host_credentials = host_netrc.netrc(path)
    assert port_credentials.hosts == host_credentials.hosts
    assert port_credentials.macros == host_credentials.macros
    for host in ("example.com", "unknown.example"):
        assert port_credentials.authenticators(host) == (
            host_credentials.authenticators(host)
        )
    assert repr(port_credentials) == repr(host_credentials)


def test_netrc_parse_error_message_matches_cpython(tmp_path):
    path = tmp_path / "bad.netrc"
    path.write_text(
        "machine example.com login user unexpected value\n",
        encoding="utf-8",
    )
    with pytest.raises(port_netrc.NetrcParseError) as port_error:
        port_netrc.netrc(path)
    with pytest.raises(host_netrc.NetrcParseError) as host_error:
        host_netrc.netrc(path)
    assert port_error.value.msg == host_error.value.msg
    assert str(port_error.value) == str(host_error.value)


def test_netrc_implicit_file_fails_closed_without_permission_metadata(
    tmp_path, monkeypatch
):
    (tmp_path / ".netrc").write_text(
        "machine example.com login user password secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(NotImplementedError, match="owner and mode checks"):
        port_netrc.netrc()


def test_gettext_null_translation_matches_cpython():
    port_translation = port_gettext.NullTranslations()
    host_translation = host_gettext.NullTranslations()
    assert port_translation.gettext("hello") == host_translation.gettext("hello")
    assert port_translation.pgettext("menu", "Open") == host_translation.pgettext(
        "menu", "Open"
    )
    for count in (0, 1, 2):
        assert port_translation.ngettext("item", "items", count) == (
            host_translation.ngettext("item", "items", count)
        )
        assert port_translation.npgettext("ctx", "item", "items", count) == (
            host_translation.npgettext("ctx", "item", "items", count)
        )
    assert port_translation.info() == host_translation.info()
    assert port_translation.charset() == host_translation.charset()


def test_gettext_null_translation_fallback_matches_cpython():
    class Fallback:
        def gettext(self, message):
            return "translated:" + message

        def ngettext(self, singular, plural, count):
            return "translated:" + (singular if count == 1 else plural)

        def pgettext(self, context, message):
            return context + ":" + message

        def npgettext(self, context, singular, plural, count):
            return context + ":" + (singular if count == 1 else plural)

    port_translation = port_gettext.NullTranslations()
    host_translation = host_gettext.NullTranslations()
    port_translation.add_fallback(Fallback())
    host_translation.add_fallback(Fallback())
    assert port_translation.gettext("hello") == host_translation.gettext("hello")
    assert port_translation.ngettext("item", "items", 2) == (
        host_translation.ngettext("item", "items", 2)
    )
    assert port_translation.pgettext("menu", "Open") == (
        host_translation.pgettext("menu", "Open")
    )
    assert port_translation.npgettext("ctx", "item", "items", 2) == (
        host_translation.npgettext("ctx", "item", "items", 2)
    )


def test_gettext_module_level_no_catalog_fallback_matches_cpython():
    domain = "pcc-build-tool-no-such-catalog"
    old_port_domain = port_gettext.textdomain()
    old_host_domain = host_gettext.textdomain()
    try:
        assert port_gettext.textdomain(domain) == host_gettext.textdomain(domain)
        assert port_gettext.gettext("hello") == host_gettext.gettext("hello")
        assert port_gettext.pgettext("menu", "Open") == host_gettext.pgettext(
            "menu", "Open"
        )
        for count in (1, 2):
            assert port_gettext.ngettext("item", "items", count) == (
                host_gettext.ngettext("item", "items", count)
            )
            assert port_gettext.npgettext("ctx", "item", "items", count) == (
                host_gettext.npgettext("ctx", "item", "items", count)
            )
        port_fallback = port_gettext.translation(domain, fallback=True)
        host_fallback = host_gettext.translation(domain, fallback=True)
        assert port_fallback.gettext("hello") == host_fallback.gettext("hello")
    finally:
        port_gettext.textdomain(old_port_domain)
        host_gettext.textdomain(old_host_domain)


def test_gettext_bound_catalogue_fails_closed(tmp_path):
    domain = "pcc-bound-catalogue"
    assert port_gettext.bindtextdomain(domain, str(tmp_path)) == (
        host_gettext.bindtextdomain(domain, str(tmp_path))
    )
    with pytest.raises(NotImplementedError, match="catalogue parsing"):
        port_gettext.dgettext(domain, "hello")


UUID_TEXT_CASES = [
    "12345678-1234-5678-9234-567812345678",
    "12345678123456789234567812345678",
    "{12345678-1234-5678-9234-567812345678}",
    "urn:uuid:12345678-1234-5678-9234-567812345678",
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
]


@pytest.mark.parametrize("text", UUID_TEXT_CASES)
def test_uuid_value_surface_matches_cpython(text):
    port_value = port_uuid.UUID(text)
    host_value = host_uuid.UUID(text)
    assert str(port_value) == str(host_value)
    assert repr(port_value) == repr(host_value)
    assert port_value.hex == host_value.hex
    assert port_value.int == host_value.int
    assert port_value.bytes == host_value.bytes
    assert port_value.bytes_le == host_value.bytes_le
    assert port_value.fields == host_value.fields
    assert port_value.time == host_value.time
    assert port_value.clock_seq == host_value.clock_seq
    assert port_value.node == host_value.node
    assert port_value.urn == host_value.urn
    assert port_value.variant == host_value.variant
    assert port_value.version == host_value.version
    assert hash(port_value) == hash(host_value)


def test_uuid_constructor_forms_and_version_override_match_cpython():
    raw = bytes.fromhex("12345678123456781234567812345678")
    fields = (0x12345678, 0x1234, 0x5678, 0x12, 0x34, 0x567812345678)
    constructors = [
        ({"bytes": raw},),
        ({"bytes_le": raw[3::-1] + raw[5:3:-1] + raw[7:5:-1] + raw[8:]},),
        ({"fields": fields},),
        ({"int": int.from_bytes(raw, "big")},),
        ({"bytes": raw, "version": 4},),
    ]
    for (kwargs,) in constructors:
        port_value = port_uuid.UUID(**kwargs)
        host_value = host_uuid.UUID(**kwargs)
        assert str(port_value) == str(host_value)
        assert port_value.int == host_value.int
        assert port_value.bytes == host_value.bytes
        assert port_value.fields == host_value.fields
        assert port_value.version == host_value.version


@pytest.mark.parametrize(
    "namespace,name",
    [
        ("NAMESPACE_DNS", "python.org"),
        ("NAMESPACE_URL", "https://mesonbuild.com/native-file.html"),
        ("NAMESPACE_OID", "1.3.6.1.4.1"),
        ("NAMESPACE_X500", "cn=example,dc=invalid"),
        ("NAMESPACE_URL", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"),
    ],
)
def test_uuid5_matches_cpython(namespace, name):
    port_value = port_uuid.uuid5(getattr(port_uuid, namespace), name)
    host_value = host_uuid.uuid5(getattr(host_uuid, namespace), name)
    assert str(port_value) == str(host_value)
    assert port_value.int == host_value.int
    assert port_value.hex == host_value.hex
    assert port_value.version == host_value.version == 5
    assert port_value.variant == host_value.variant == port_uuid.RFC_4122


def test_uuid4_has_cpython_version_variant_and_value_shape():
    value = port_uuid.uuid4()
    assert isinstance(value, port_uuid.UUID)
    assert value.version == 4
    assert value.variant == port_uuid.RFC_4122
    assert len(value.hex) == 32
    assert len(str(value)) == 36
    assert value.hex == str(value).replace("-", "")
    assert 0 <= value.int < (1 << 128)


def test_uuid_unowned_algorithms_fail_closed():
    with pytest.raises(NotImplementedError, match="clock state"):
        port_uuid.uuid1()
    with pytest.raises(NotImplementedError, match="correct native MD5"):
        port_uuid.uuid3(port_uuid.NAMESPACE_URL, "name")
    with pytest.raises(NotImplementedError, match="hardware-address"):
        port_uuid.getnode()


CONFIG_TEXT = """\
[DEFAULT]
root = /srv/project
jobs = 4
enabled = yes
ratio = 1.5

[build]
path = %(root)s/out
MixedKey = value
lines = first
  second

[provide]
dependency_names = zlib, libpng
"""


def _parser_pair(**kwargs):
    port_parser = port_configparser.ConfigParser(**kwargs)
    host_parser = host_configparser.ConfigParser(**kwargs)
    port_parser.read_string(CONFIG_TEXT, "meson.ini")
    host_parser.read_string(CONFIG_TEXT, "meson.ini")
    return port_parser, host_parser


def test_configparser_meson_read_and_typed_getters_match_cpython():
    port_parser, host_parser = _parser_pair()
    assert port_parser.sections() == host_parser.sections()
    assert port_parser.defaults() == host_parser.defaults()
    assert port_parser.options("build") == host_parser.options("build")
    assert port_parser.items("build") == host_parser.items("build")
    assert port_parser.get("build", "path") == host_parser.get("build", "path")
    assert port_parser.get("build", "path", raw=True) == host_parser.get(
        "build", "path", raw=True
    )
    assert port_parser.get(
        "build", "path", vars={"root": "/override"}
    ) == host_parser.get("build", "path", vars={"root": "/override"})
    assert port_parser.getint("build", "jobs") == host_parser.getint(
        "build", "jobs"
    )
    assert port_parser.getfloat("build", "ratio") == host_parser.getfloat(
        "build", "ratio"
    )
    assert port_parser.getboolean("build", "enabled") == host_parser.getboolean(
        "build", "enabled"
    )
    assert port_parser.get("build", "missing", fallback=None) is None
    assert host_parser.get("build", "missing", fallback=None) is None


def test_configparser_raw_and_case_preserving_subclass_match_cpython():
    class PortCaseParser(port_configparser.ConfigParser):
        def optionxform(self, option):
            return option

    class HostCaseParser(host_configparser.ConfigParser):
        def optionxform(self, option):
            return option

    text = "[cross]\nSubProject:Option = %(literal)s\n"
    port_parser = PortCaseParser(delimiters=("=",), interpolation=None)
    host_parser = HostCaseParser(delimiters=("=",), interpolation=None)
    port_parser.read_string(text, "machine.ini")
    host_parser.read_string(text, "machine.ini")
    assert port_parser.items("cross") == host_parser.items("cross")
    assert port_parser.get("cross", "SubProject:Option") == host_parser.get(
        "cross", "SubProject:Option"
    )

    port_raw = port_configparser.RawConfigParser()
    host_raw = host_configparser.RawConfigParser()
    port_raw.read_string("[s]\nvalue = %(not-expanded)s\n")
    host_raw.read_string("[s]\nvalue = %(not-expanded)s\n")
    assert port_raw.get("s", "value") == host_raw.get("s", "value")

    multiline = "[s]\nempty =\n  second # ignored\n"
    port_inline = port_configparser.ConfigParser(
        inline_comment_prefixes=("#",)
    )
    host_inline = host_configparser.ConfigParser(
        inline_comment_prefixes=("#",)
    )
    port_inline.read_string(multiline)
    host_inline.read_string(multiline)
    assert port_inline.get("s", "empty") == host_inline.get("s", "empty")


def test_configparser_section_proxy_and_mutation_match_cpython():
    port_parser = port_configparser.ConfigParser({"base": "/opt"})
    host_parser = host_configparser.ConfigParser({"base": "/opt"})
    payload = {
        "alpha": {"One": 1, "enabled": "off"},
        "beta": {"path": "%(base)s/lib"},
    }
    port_parser.read_dict(payload, source="generated")
    host_parser.read_dict(payload, source="generated")
    port_parser["alpha"]["added"] = "value"
    host_parser["alpha"]["added"] = "value"
    port_parser["gamma"] = {"answer": "42"}
    host_parser["gamma"] = {"answer": "42"}

    for section in ("alpha", "beta", "gamma"):
        assert dict(port_parser[section]) == dict(host_parser[section])
    with pytest.raises(AttributeError):
        port_parser["alpha"][0]
    with pytest.raises(AttributeError):
        host_parser["alpha"][0]
    assert port_parser["alpha"].getboolean("enabled") == host_parser[
        "alpha"
    ].getboolean("enabled")
    assert port_parser["gamma"].getint("answer") == host_parser["gamma"].getint(
        "answer"
    )
    assert port_parser.remove_option("alpha", "added") == host_parser.remove_option(
        "alpha", "added"
    )
    assert port_parser.remove_section("gamma") == host_parser.remove_section(
        "gamma"
    )
    assert port_parser.sections() == host_parser.sections()


def test_configparser_read_files_and_missing_files_match_cpython(tmp_path):
    first = tmp_path / "first.ini"
    second = tmp_path / "second.ini"
    missing = tmp_path / "missing.ini"
    first.write_text("[build]\nvalue = one\n", encoding="utf-8")
    second.write_text("[build]\nvalue = two\n[extra]\nflag = on\n", encoding="utf-8")
    port_parser = port_configparser.ConfigParser(interpolation=None)
    host_parser = host_configparser.ConfigParser(interpolation=None)
    assert port_parser.read([missing, first, second], encoding="utf-8") == (
        host_parser.read([missing, first, second], encoding="utf-8")
    )
    assert port_parser.sections() == host_parser.sections()
    assert port_parser.items("build") == host_parser.items("build")


def test_configparser_write_matches_cpython():
    port_parser, host_parser = _parser_pair()
    port_output = io.StringIO()
    host_output = io.StringIO()
    port_parser.write(port_output)
    host_parser.write(host_output)
    assert port_output.getvalue() == host_output.getvalue()


@pytest.mark.parametrize(
    "text,error_name",
    [
        ("key = value\n", "MissingSectionHeaderError"),
        ("[s]\na = 1\na = 2\n", "DuplicateOptionError"),
        ("[s]\na = 1\n[s]\nb = 2\n", "DuplicateSectionError"),
    ],
)
def test_configparser_strict_errors_match_cpython(text, error_name):
    port_error_type = getattr(port_configparser, error_name)
    host_error_type = getattr(host_configparser, error_name)
    with pytest.raises(port_error_type) as port_error:
        port_configparser.ConfigParser().read_string(text, "bad.ini")
    with pytest.raises(host_error_type) as host_error:
        host_configparser.ConfigParser().read_string(text, "bad.ini")
    assert str(port_error.value) == str(host_error.value)


def test_configparser_missing_option_and_bad_boolean_match_cpython():
    port_parser, host_parser = _parser_pair()
    with pytest.raises(port_configparser.NoOptionError) as port_error:
        port_parser.get("build", "missing")
    with pytest.raises(host_configparser.NoOptionError) as host_error:
        host_parser.get("build", "missing")
    assert str(port_error.value) == str(host_error.value)

    port_parser.set("build", "enabled", "sometimes")
    host_parser.set("build", "enabled", "sometimes")
    with pytest.raises(ValueError) as port_boolean_error:
        port_parser.getboolean("build", "enabled")
    with pytest.raises(ValueError) as host_boolean_error:
        host_parser.getboolean("build", "enabled")
    assert str(port_boolean_error.value) == str(host_boolean_error.value)


def test_configparser_unowned_extension_points_fail_closed():
    with pytest.raises(NotImplementedError, match="dict_type"):
        port_configparser.ConfigParser(dict_type=list)
    with pytest.raises(NotImplementedError, match="converters"):
        port_configparser.ConfigParser(converters={"decimal": float})
    parser = port_configparser.ConfigParser(
        interpolation=port_configparser.ExtendedInterpolation()
    )
    with pytest.raises(NotImplementedError, match="cross-section"):
        parser.read_string("[section]\nvalue = ${other:value}\n")
    legacy = port_configparser.ConfigParser(
        interpolation=port_configparser.LegacyInterpolation()
    )
    with pytest.raises(NotImplementedError, match="LegacyInterpolation"):
        legacy.read_string("[section]\nvalue = literal\n")
    valueless = port_configparser.ConfigParser(allow_no_value=True)
    with pytest.raises(NotImplementedError, match="valueless"):
        valueless.read_string("[section]\nvalue\n  continuation\n")


def _explicit_sysconfig_vars():
    return {
        "base": "/prefix",
        "platbase": "/plat-prefix",
        "installed_base": "/installed",
        "installed_platbase": "/plat-installed",
        "platlibdir": "lib",
        "py_version_short": "9.8",
        "py_version_nodot": "98",
        "py_version_nodot_plat": "98",
        "abiflags": "",
        "userbase": "/user",
    }


def test_sysconfig_scheme_inventory_and_expansion_match_cpython():
    assert port_sysconfig.get_scheme_names() == host_sysconfig.get_scheme_names()
    assert port_sysconfig.get_path_names() == host_sysconfig.get_path_names()
    assert port_sysconfig.get_default_scheme() == host_sysconfig.get_default_scheme()
    port_paths = port_sysconfig.get_paths(
        "posix_prefix", vars=_explicit_sysconfig_vars()
    )
    host_paths = host_sysconfig.get_paths(
        "posix_prefix", vars=_explicit_sysconfig_vars()
    )
    assert port_paths == host_paths
    for name in port_paths:
        assert port_sysconfig.get_path(
            name, "posix_prefix", vars=_explicit_sysconfig_vars()
        ) == host_sysconfig.get_path(
            name, "posix_prefix", vars=_explicit_sysconfig_vars()
        )
    assert port_sysconfig.get_paths("posix_prefix", expand=False) == (
        host_sysconfig.get_paths("posix_prefix", expand=False)
    )


def test_sysconfig_owned_config_vars_match_stable_cpython_surface():
    assert port_sysconfig.get_python_version() == host_sysconfig.get_python_version()
    keys = ("prefix", "exec_prefix", "py_version_short", "VERSION")
    assert port_sysconfig.get_config_vars(*keys) == host_sysconfig.get_config_vars(
        *keys
    )
    for key in keys:
        assert port_sysconfig.get_config_var(key) == host_sysconfig.get_config_var(key)
    assert port_sysconfig.get_config_var("PCC_UNKNOWN_CONFIG_VAR") is None
    assert host_sysconfig.get_config_var("PCC_UNKNOWN_CONFIG_VAR") is None
    assert isinstance(port_sysconfig.get_platform(), str)
    assert bool(port_sysconfig.get_platform()) == bool(host_sysconfig.get_platform())


def test_sysconfig_unowned_cpython_build_metadata_fails_closed():
    with pytest.raises(NotImplementedError, match="Makefile"):
        port_sysconfig.get_makefile_filename()
    with pytest.raises(NotImplementedError, match="pyconfig.h"):
        port_sysconfig.get_config_h_filename()
    with pytest.raises(NotImplementedError, match="Makefile variable"):
        port_sysconfig.expand_makefile_vars("$(prefix)/lib", {"prefix": "/usr"})
    with pytest.raises(NotImplementedError, match="source-tree"):
        port_sysconfig.is_python_build(check_home=True)


def test_signal_constants_names_and_valid_set_match_cpython():
    names = ("SIGINT", "SIGTERM", "SIGKILL", "SIGABRT", "SIGSEGV")
    for name in names:
        port_value = getattr(port_signal, name)
        host_value = getattr(host_signal, name)
        assert int(port_value) == int(host_value)
        assert port_signal.Signals(port_value).name == host_signal.Signals(
            host_value
        ).name
    assert port_signal.NSIG == host_signal.NSIG
    assert {int(value) for value in port_signal.valid_signals()} == {
        int(value) for value in host_signal.valid_signals()
    }


def test_signal_unowned_python_callback_boundary_fails_closed():
    def callback(signalnum, frame):
        return None

    with pytest.raises(NotImplementedError, match="Python signal callbacks"):
        port_signal.signal(port_signal.SIGINT, callback)
    with pytest.raises(KeyboardInterrupt):
        port_signal.default_int_handler(port_signal.SIGINT, None)


def test_runpy_public_inventory_matches_cpython():
    assert port_runpy.__all__ == host_runpy.__all__


def test_runpy_dynamic_execution_boundary_fails_closed():
    with pytest.raises(NotImplementedError, match="fresh-namespace"):
        port_runpy.run_module("example", run_name="__main__", alter_sys=True)
    with pytest.raises(NotImplementedError, match="fresh-namespace"):
        port_runpy.run_path("example.py", run_name="__main__")


def test_pwd_struct_passwd_sequence_surface_matches_cpython():
    host_record = host_pwd.getpwuid(os.getuid())
    port_record = port_pwd.struct_passwd(tuple(host_record))
    assert len(port_record) == len(host_record)
    assert tuple(port_record) == tuple(host_record)
    assert port_record == tuple(host_record)
    assert port_record == port_pwd.struct_passwd(tuple(host_record))
    assert repr(port_record) == repr(host_record)
    assert hash(port_record) == hash(host_record)
    assert port_pwd.struct_passwd.n_fields == host_pwd.struct_passwd.n_fields
    assert port_pwd.struct_passwd.n_sequence_fields == (
        host_pwd.struct_passwd.n_sequence_fields
    )
    assert port_pwd.struct_passwd.n_unnamed_fields == (
        host_pwd.struct_passwd.n_unnamed_fields
    )
    for name in (
        "pw_name",
        "pw_passwd",
        "pw_uid",
        "pw_gid",
        "pw_gecos",
        "pw_dir",
        "pw_shell",
    ):
        assert getattr(port_record, name) == getattr(host_record, name)
    with pytest.raises(AttributeError):
        port_record.pw_dir = "/changed"


def test_pwd_process_global_enumeration_fails_closed():
    with pytest.raises(NotImplementedError, match="serialized NSS enumeration"):
        port_pwd.getpwall()


def test_pwd_lookup_input_validation_matches_cpython():
    for module in (port_pwd, host_pwd):
        with pytest.raises(TypeError):
            module.getpwnam(b"root")
        with pytest.raises(ValueError):
            module.getpwnam("root\x00suffix")
        with pytest.raises(TypeError):
            module.getpwuid(1.5)


@pytest.mark.parametrize(
    "module_name",
    [
        "codecs",
        "locale",
        "pprint",
        "filecmp",
        "difflib",
        "netrc",
        "gettext",
        "uuid",
        "configparser",
        "sysconfig",
        "signal",
        "runpy",
        "pwd",
        "ntpath",
        "msvcrt",
    ],
)
def test_build_tool_ports_are_selected_by_recursive_stdlib_registry(module_name):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source(module_name)
    assert source is not None
    assert source.endswith("/pcc/stdlib/" + module_name + ".py")
    assert module_name not in pipeline._NATIVE_BUILTIN_IMPORTS


def test_windows_msvcrt_provider_is_compile_owned_but_non_windows_fail_closed(tmp_path):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source("msvcrt")
    assert source is not None
    with open(source, "r", encoding="utf-8") as stream:
        provider_text = stream.read()
    assert 'if sys.platform != "win32"' in provider_text
    assert 'raise ImportError("No module named \'msvcrt\'")' in provider_text

    entry = tmp_path / "conditional_windows_import.py"
    entry.write_text(
        'import sys\nif sys.platform == "win32":\n    import msvcrt\n',
        encoding="utf-8",
    )
    seed_sources, seed_modules = pipeline._collect_relative_module_closure(
        str(entry)
    )
    _sources, modules = pipeline._collect_multi_source_relative_closure(
        seed_sources,
        seed_modules,
        recursive_stdlib=True,
    )
    assert "msvcrt" in modules


def test_type_checking_http_client_is_not_a_runtime_stdlib_dependency(tmp_path):
    from pcc.frontends.python import pipeline

    source = (
        "import typing as T\n"
        "from typing import TYPE_CHECKING as TC\n"
        "from typing import (\n"
        "    TYPE_CHECKING as TP,\n"
        "    Any,\n"
        ")\n"
        "if T.TYPE_CHECKING:\n"
        "    import http.client\n"
        "if TC: import http.server\n"
        "if TP:\n"
        "    import http.cookiejar\n"
        "import ntpath\n"
    )
    assert pipeline._source_absolute_imports_for_discovery(source) == [
        "typing",
        "ntpath",
    ]
    probe = tmp_path / "type_only_http.py"
    probe.write_text(source, encoding="utf-8")
    discovered = pipeline._stdlib_absolute_imports_in(str(probe))
    assert "http.client" not in discovered
    assert "http.server" not in discovered
    assert "http.cookiejar" not in discovered
    assert "ntpath" in discovered

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    meson_wrap = os.path.join(
        repo_root,
        "projects",
        "numpy-2.4.4",
        "vendored-meson",
        "meson",
        "mesonbuild",
        "wrap",
        "wrap.py",
    )
    if os.path.isfile(meson_wrap):
        assert "http.client" not in pipeline._stdlib_absolute_imports_in(
            meson_wrap
        )


def test_measured_build_tool_gap_has_exact_owned_or_excluded_disposition():
    from pcc.frontends.python import pipeline

    assert len(MEASURED_BUILD_TOOL_GAP) == 34
    excluded = {"http"}
    owned = MEASURED_BUILD_TOOL_GAP - excluded
    for module_name in sorted(owned):
        source = pipeline._locate_native_stdlib_module_source(module_name)
        assert source is not None, module_name
        normalized = source.replace("\\", "/")
        assert "/pcc/stdlib/" in normalized, (module_name, source)
    assert pipeline._locate_native_stdlib_module_source("http") is None


# --- compiled-mode contract -------------------------------------------------
# The differential tests above run under CPython, where these modules are just
# ordinary Python. The contract that actually matters is that pcc can COMPILE
# them and that the compiled program produces the same answers with no
# libpython fallback. Two real gaps were only visible here: ``textwrap`` was on
# the pipeline's module skip-list (so every entry point except the natively
# lowered ``dedent`` fell back), and fnmatch matching through ``re`` raised
# NotImplementedError because the translated pattern leaves pcc's regex subset.


_COMPILED_PROBE = '''\
import errno
import stat
import fnmatch
import textwrap
import posixpath
import codecs
import locale
import pprint
import filecmp
import difflib
import netrc
import gettext
import uuid
import configparser
import sysconfig
import signal
import runpy
import pwd
import ntpath
import sys
if sys.platform == "win32":
    import msvcrt
from configparser import ConfigParser as ImportedConfigParser
from configparser import MissingSectionHeaderError, RawConfigParser

print("errno", errno.ENOENT, errno.EAGAIN)
print("stat", stat.S_ISDIR(0o040755), stat.S_ISREG(0o100644))
print("fnmatch", fnmatch.fnmatch("a.txt", "*.txt"), fnmatch.fnmatch("a.txt", "*.py"))
print("filter", fnmatch.filter(["a.py", "b.txt"], "*.py"))
print("dedent", repr(textwrap.dedent("  a\\n  b\\n")))
print("wrap", textwrap.wrap("the quick brown fox", 10))
print("indent", repr(textwrap.indent("a\\nb\\n", ">")))
print("shorten", textwrap.shorten("hello world again", 12))
print("dirname", posixpath.dirname("/a//b"))
print("split", posixpath.split("/a/b"))
print(
    "ntpath",
    ntpath.sep,
    ntpath.altsep,
    ntpath.splitdrive(r"C:\\build\\meson.py"),
    ntpath.normpath(r"C:\\build\\.\\meson\\..\\out"),
    ntpath.join(r"\\\\server\\share", "meson", "out"),
)
print("msvcrt-branch", sys.platform != "win32")
print("codec-decode", repr(
    codecs.decode(b"\\\\u00e9\\\\n", "unicode_escape")
))
print("codec-encode", codecs.encode(
    "caf\\N{LATIN SMALL LETTER E WITH ACUTE}\\n", "unicode_escape"
).decode())
print("codec-bom", codecs.BOM_UTF8 == b"\\xef\\xbb\\xbf")
print("locale", locale.getpreferredencoding(False).lower().replace(
    "-", ""
).replace("_", ""))
print("pformat", repr(pprint.pformat(
    {"b": [0, 1, 2, 3, 4, 5], "a": 1}, width=18, compact=True
)))
print("difflib", difflib.SequenceMatcher(None, "abxcd", "abcd").get_opcodes())
print("ndiff", list(difflib.ndiff(["one\\n"], ["ore\\n"])))
print("gettext", gettext.gettext("hello"), gettext.ngettext("item", "items", 2))
print("null-gettext", gettext.NullTranslations().pgettext("menu", "Open"))
stable_uuid = uuid.uuid5(uuid.NAMESPACE_URL, "meson-vs-build:project")
random_uuid = uuid.uuid4()
print("uuid5", str(stable_uuid), stable_uuid.hex, stable_uuid.int)
print(
    "uuid4-shape",
    len(str(random_uuid)),
    len(random_uuid.hex),
    random_uuid.version,
    random_uuid.variant == uuid.RFC_4122,
    random_uuid.hex == str(random_uuid).replace("-", ""),
    0 <= random_uuid.int < (1 << 128),
)
config = configparser.ConfigParser(interpolation=None, delimiters=("=",))
config.read_string(
    "[cross]\\nSubProject:Option = literal\\n[flags]\\nenabled = yes\\njobs = 4\\n"
)
print("configparser", config.sections(), config.items("cross"))
print(
    "configparser-types",
    config.getboolean("flags", "enabled"),
    config.getint("flags", "jobs"),
    dict(config["cross"]),
)
raw_config = RawConfigParser()
raw_config.read_string("[raw]\\nvalue = %(literal)s\\n")
print("configparser-raw", raw_config.get("raw", "value"))
try:
    ImportedConfigParser().read_string("value = no-section\\n", "broken.ini")
except MissingSectionHeaderError:
    print("configparser-error", True)
path_vars = {
    "base": "/prefix",
    "platbase": "/plat-prefix",
    "installed_base": "/installed",
    "installed_platbase": "/plat-installed",
    "platlibdir": "lib",
    "py_version_short": "9.8",
    "py_version_nodot": "98",
    "py_version_nodot_plat": "98",
    "abiflags": "",
    "userbase": "/user",
}
paths = sysconfig.get_paths("posix_prefix", vars=path_vars)
print("sysconfig-paths", paths["purelib"], paths["include"], paths["scripts"])
raw_paths = sysconfig.get_paths("posix_prefix", expand=False)
config_vars = sysconfig.get_config_vars()
print(
    "sysconfig-owned",
    sysconfig.get_config_var("PCC_UNKNOWN_CONFIG_VAR") is None,
    sysconfig.get_config_var("VERSION") == sysconfig.get_python_version(),
    sysconfig.get_default_scheme() in sysconfig.get_scheme_names(),
    bool(sysconfig.get_platform()),
    config_vars.get("VERSION") == sysconfig.get_python_version(),
    "purelib" in raw_paths,
)
print(
    "signal",
    signal.SIGINT,
    signal.SIGTERM,
    signal.Signals(signal.SIGINT).name,
    signal.SIGKILL in signal.valid_signals(),
    signal.NSIG,
    signal.strsignal(signal.SIGTERM),
)
previous_disposition = signal.signal(signal.SIGUSR1, signal.SIG_IGN)
ignored_disposition = signal.getsignal(signal.SIGUSR1)
signal.signal(signal.SIGUSR1, previous_disposition)
print(
    "signal-disposition",
    previous_disposition in (signal.SIG_DFL, signal.SIG_IGN),
    ignored_disposition == signal.SIG_IGN,
)
print("runpy-api", runpy.__all__)
'''


@pytest.mark.integration
def test_build_tool_stdlib_modules_compile_and_match_cpython(tmp_path):
    import subprocess
    import sys

    cmp_left = tmp_path / "cmp_left.bin"
    cmp_same = tmp_path / "cmp_same.bin"
    cmp_different = tmp_path / "cmp_different.bin"
    cmp_left.write_bytes(b"a" * 9000 + b"tail")
    cmp_same.write_bytes(b"a" * 9000 + b"tail")
    cmp_different.write_bytes(b"a" * 9000 + b"fail")
    netrc_path = tmp_path / "compiled.netrc"
    netrc_path.write_text(
        "machine example.com login user account build password secret\n"
        "default login guest password fallback\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "compiled.ini"
    config_path.write_text("[disk]\nvalue = from-file\n", encoding="utf-8")
    missing_config_path = tmp_path / "missing.ini"
    written_config_path = tmp_path / "written.ini"
    host_passwd = host_pwd.getpwuid(os.getuid())
    probe = _COMPILED_PROBE + (
        'print("filecmp", filecmp.cmp('
        + repr(str(cmp_left))
        + ", "
        + repr(str(cmp_same))
        + ', shallow=False), filecmp.cmp('
        + repr(str(cmp_left))
        + ", "
        + repr(str(cmp_different))
        + ", shallow=False))\n"
        + "credentials = netrc.netrc("
        + repr(str(netrc_path))
        + ")\n"
        + 'print("netrc", "example.com" in credentials.hosts, '
        + 'credentials.authenticators("example.com"), '
        + 'credentials.authenticators("missing"))\n'
        + "disk_config = ImportedConfigParser(interpolation=None)\n"
        + 'print("configparser-file", disk_config.read(['
        + repr(str(missing_config_path))
        + ", "
        + repr(str(config_path))
        + '], encoding="utf-8"), disk_config.get("disk", "value"))\n'
        + "with open("
        + repr(str(written_config_path))
        + ', "w", encoding="utf-8") as config_stream:\n'
        + "    disk_config.write(config_stream)\n"
        + "written_config = ImportedConfigParser(interpolation=None)\n"
        + "written_config.read("
        + repr(str(written_config_path))
        + ', encoding="utf-8")\n'
        + 'print("configparser-write", written_config.get("disk", "value"))\n'
        + "passwd_by_uid = pwd.getpwuid("
        + str(host_passwd.pw_uid)
        + ")\n"
        + "passwd_by_name = pwd.getpwnam("
        + repr(host_passwd.pw_name)
        + ")\n"
        + 'print("pwd", passwd_by_uid.pw_name, passwd_by_uid.pw_uid, '
        + "passwd_by_uid.pw_gid, passwd_by_uid.pw_dir, "
        + "passwd_by_uid.pw_shell, "
        + "passwd_by_name.pw_uid == passwd_by_uid.pw_uid, "
        + "passwd_by_name.pw_dir == passwd_by_uid.pw_dir)\n"
    )
    src = tmp_path / "probe.py"
    src.write_text(probe, encoding="utf-8")
    exe = tmp_path / "probe_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)

    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(src), "-o", str(exe)],
        text=True, capture_output=True, timeout=900, env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert "PCC-PY-COMPILE-001" not in (build.stdout + build.stderr), (
        "these modules must lower natively, not through a libpython fallback"
    )

    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PATH"] = str(tmp_path / "no-host-python")
    got = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=120, env=run_env
    )
    assert got.returncode == 0, got.stdout + got.stderr

    # Same program under CPython must print exactly the same thing.
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert expected.returncode == 0, expected.stderr
    assert got.stdout == expected.stdout, (
        "compiled output differs from CPython\n"
        f"pcc:\n{got.stdout}\ncpython:\n{expected.stdout}"
    )
