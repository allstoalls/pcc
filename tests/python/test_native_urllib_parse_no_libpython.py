from __future__ import annotations

import os
import subprocess

import pytest


_URLJOIN_NATIVE_PROBE = """import urllib.parse
import gc
def resolve(base, ref, fragments=True):
    return urllib.parse.urljoin(base, ref, fragments)
def report(value):
    print(repr(value))
    return value
def invoke(callback, base, ref):
    page_url = resolve(base, ref)
    return callback(page_url)
def main():
    invoke(report, 'https://example.test/a/b', '../c?x#f')
    invoke(report, b'https://example.test/a/b', b'../c?x#f')
    for ref in ('?', '#', '?#', '../../d/', '//other/path', 'http:g', 'é'):
        report(resolve('http://a/b/c?q#f', ref))
    report(resolve('http://a/b/c?q#f', '#part', False))
    report(resolve('http://a/b', '//[::ffff:192.0.2.1]/c'))
    report(resolve('http://a/b', '//[v1.future:host]/c'))
    report(resolve('http://a/b', '//é.example/c'))
    report(resolve('http://a/b', '\\x00 ../c\\t'))
    try:
        resolve('http://a/b', b'c')
    except TypeError:
        print('mixed types')
    for ref in ('//[1::2::3]/c', '//example.com\\uff0fother/c'):
        try:
            resolve('http://a/b', ref)
        except ValueError:
            print('invalid authority')
    gc.collect()
main()
"""


@pytest.mark.integration
def test_urljoin_owned_native_result_handoff(tmp_path, monkeypatch, pcc_runtime_archive):
    """Native execution gate; host source/IR controls cannot substitute for it."""
    import hashlib
    import json
    import sys
    from pcc.frontends.python.pipeline import compile_python
    from pcc.diagnostics.gc_log import parse_log_lines

    source = tmp_path / "urljoin_native.py"
    executable = tmp_path / "urljoin_native"
    source.write_text(_URLJOIN_NATIVE_PROBE, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], text=True, capture_output=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("PCC_HOST_PYTHON", sys.executable)
    # The archive fixture verifies source/target/configuration provenance.
    # Runtime selection is supported, so all collectors run the same binary.
    compile_python(
        str(source), str(executable), backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    binary_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    receipts = []
    for collector in range(5):
        log = tmp_path / ("urljoin-gc" + str(collector) + ".jsonl")
        env = dict(os.environ, PCC_GC_BACKEND=str(collector), PCC_LOG="gc",
                   PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log))
        run = subprocess.run(
            [str(executable)], text=True, capture_output=True, timeout=30, env=env,
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout == reference.stdout
        assert run.stderr == reference.stderr
        assert log.is_file(), "collection must provide an observed backend witness"
        events = parse_log_lines(log.read_text(encoding="utf-8").splitlines())
        observed = sorted({event.fields["value1"] for event in events
                           if event.fields.get("category") == "gc"
                           and event.event in ("collect_start", "collect_stop", "collect_end")})
        receipts.append({"requested_backend": collector, "observed_backends": observed,
                         "binary_sha256": binary_hash})
        (tmp_path / "urljoin-native-receipts.json").write_text(
            json.dumps(receipts, indent=2) + "\n", encoding="utf-8",
        )
        assert observed == [collector], receipts[-1]
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == binary_hash


def _owned_urlparse_provider():
    from pcc.stdlib.urllib import parse

    return parse


@pytest.mark.parametrize("reference", [
    "g:h", "g", "./g", "g/", "/g", "//g", "?y", "g?y", "#s", "g#s",
    "g?y#s", ";x", "g;x", "g;x?y#s", "", ".", "./", "..", "../",
    "../g", "../..", "../../", "../../g", "../../../g", "../../../../g",
    "/./g", "/../g", "g.", ".g", "g..", "..g", "./../g", "./g/.",
    "g/./h", "g/../h", "g;x=1/./y", "g;x=1/../y", "g?y/./x",
    "g?y/../x", "g#s/./x", "g#s/../x", "http:g", "http:", "http:/g",
    "https://other/x", "HTTP://other/x", "?", "#", "?#", "//", "///g",
    "//other/a/../b", "/a//b", "a//b", "a///", "\t ../g\n", "1:g",
    "é:g", "a_b:g", "a+b.c-d:g", "//[::1]/g", "//[vF.future:host]/g",
    "//[::ffff:192.0.2.1]/g", "//[fe80::1%scope]/g", "//é.example/g",
    "//user:password@host:80/g", "\x00\x1f g ", "g%2F..%2Fh",
])
def test_urljoin_owned_source_reference_matrix(reference):
    import urllib.parse

    provider = _owned_urlparse_provider()
    for base in ("http://a/b/c/d;p?q#f", "file:///a/b", "a/b/c", "mailto:a@b"):
        for fragments in (False, True):
            expected = urllib.parse.urljoin(base, reference, fragments)
            actual = provider.urljoin(base, reference, fragments)
            assert actual == expected, (base, reference, fragments)
            if base.isascii() and reference.isascii():
                assert provider.urljoin(base.encode(), reference.encode(), fragments) == (
                    urllib.parse.urljoin(base.encode(), reference.encode(), fragments)
                )


@pytest.mark.parametrize("base,reference", [
    ("http://a/", b"b"), (b"http://a/", "b"), (b"http://a/", b"\xff"),
    (b"\xff", b"b"), (23, 42), ("http://a/", 1), ([1], [2]),
    ("http://[::1/", "b"), ("http://a/", "//host]/b"),
    ("http://a/", "//[127.0.0.1]/b"), ("http://a/", "//[not-ip]/b"),
    ("http://a/", "//[1::2::3]/b"), ("http://a/", "//[192.0.2.1::]/b"),
    ("http://a/", "//[::ffff:192.00.2.1]/b"),
    ("http://a/", "//[fe80::1%]/b"), ("http://a/", "//[fe80::1%a%b]/b"),
    ("http://a/", "//prefix[::1]/b"), ("http://a/", "//[::1]suffix/b"),
    ("http://a/", "//[v1.]/b"), ("http://a/", "//[vz.host]/b"),
    ("http://a/", "//example.com\uff0fevil/b"),
    ("http://a/", "//user\uff20host/b"), ("http://a/", "//a\u2100b/b"),
])
def test_urljoin_owned_source_error_parity(base, reference):
    import urllib.parse

    with pytest.raises(Exception) as expected:
        urllib.parse.urljoin(base, reference)
    with pytest.raises(type(expected.value)) as actual:
        _owned_urlparse_provider().urljoin(base, reference)
    assert str(actual.value) == str(expected.value)


def test_urljoin_owned_source_identity_and_type_contract():
    import urllib.parse

    provider = _owned_urlparse_provider()
    sentinel = object()
    for empty in ("", b"", None, False, 0, [], ()):
        assert provider.urljoin(empty, sentinel) is sentinel
        assert provider.urljoin(sentinel, empty) is sentinel
    for base, reference in ((bytearray(b"http://a/x"), bytearray(b"b")),
                            (bytearray(b"http://a/x"), b"b")):
        expected = urllib.parse.urljoin(base, reference)
        actual = provider.urljoin(base, reference)
        assert actual == expected and type(actual) is type(expected)

    class Text(str):
        pass

    base = Text("http://a/x")
    absolute = Text("https://b/x")
    assert provider.urljoin(base, absolute) is absolute

    class FragmentFlag:
        def __init__(self):
            self.count = 0

        def __bool__(self):
            self.count += 1
            return self.count == 1

    expected_flag, actual_flag = FragmentFlag(), FragmentFlag()
    assert provider.urljoin("http://a/x#base", "#next", actual_flag) == (
        urllib.parse.urljoin("http://a/x#base", "#next", expected_flag)
    )
    assert actual_flag.count == expected_flag.count == 2


def test_urljoin_owned_source_scheme_extensions():
    import urllib.parse

    provider = _owned_urlparse_provider()
    for module in (provider, urllib.parse):
        module.uses_relative.append("example")
        module.uses_netloc.append("example")
    try:
        assert provider.urljoin("example://a/x/y", "../z") == (
            urllib.parse.urljoin("example://a/x/y", "../z")
        )
    finally:
        for module in (provider, urllib.parse):
            module.uses_relative.remove("example")
            module.uses_netloc.remove("example")


def test_urljoin_owned_source_generated_reference_cases():
    import random
    import urllib.parse

    provider = _owned_urlparse_provider()
    rng = random.Random(616)
    components = ("a", "", ".", "..", "x;y", "%2e", "é", "x:y")
    schemes = ("http:", "ftp:", "custom:", "", "1:")
    authorities = ("//host", "//[::1]", "", "///", "//user@host:90")
    suffixes = ("", "?", "#", "?x", "#y", "?#", "?q#f")
    for _ in range(3000):
        base = rng.choice(schemes) + rng.choice(authorities) + "/a/b?q#f"
        reference = rng.choice(schemes) + rng.choice(authorities)
        reference += "/".join(rng.choice(components) for _ in range(rng.randrange(5)))
        reference += rng.choice(suffixes)
        fragments = rng.choice((True, False))
        try:
            expected = urllib.parse.urljoin(base, reference, fragments)
        except Exception as expected_error:
            with pytest.raises(type(expected_error)) as actual_error:
                provider.urljoin(base, reference, fragments)
            assert str(actual_error.value) == str(expected_error)
        else:
            assert provider.urljoin(base, reference, fragments) == expected, (
                base, reference, fragments,
            )


def test_urljoin_owned_source_nfkc_delimiter_table():
    import unicodedata

    provider = _owned_urlparse_provider()
    expected = "".join(
        chr(codepoint) for codepoint in range(128, 0x110000)
        if any(delimiter in unicodedata.normalize("NFKC", chr(codepoint))
               for delimiter in "/?#@:")
    )
    assert provider._URL_NFKC_DELIMITERS == expected


def test_urlparse_parse_result_attrs_no_libpython(tmp_path):
    src = tmp_path / "prog.py"
    src.write_text(
        "import urllib.parse\n"
        "def show(text):\n"
        "    u = urllib.parse.urlparse(text)\n"
        "    print(u.scheme, u.netloc, u.path, u.params, u.query, u.fragment)\n"
        "def rewrite(text):\n"
        "    u = urllib.parse.urlparse(text)\n"
        "    print(u._replace(netloc='', scheme='').geturl())\n"
        "def parts(text):\n"
        "    u = urllib.parse.urlparse(text)\n"
        "    print(u.username, u.password, u.hostname, u.port)\n"
        "def main():\n"
        "    show('s://:8081')\n"
        "    show('s://100.118.195.46:8087')\n"
        "    show('s://user@host:9/path,plugin@bind?rule#auth')\n"
        "    rewrite('http://example.com/')\n"
        "    rewrite('http://example.com/a?b=1#frag')\n"
        "    parts('http://example.com/')\n"
        "    parts('http://user:pass@Example.COM:8080/path')\n"
        "    parts('s://:8081')\n"
        "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "prog"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_GC_BACKEND"] = "4"
    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=420,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "s :8081    ",
        "s 100.118.195.46:8087    ",
        "s user@host:9 /path,plugin@bind  rule auth",
        "/",
        "/a?b=1#frag",
        "None None example.com None",
        "user pass example.com 8080",
        "None None None 8081",
    ]

def test_urlquote_keyword_codec_args_no_libpython(tmp_path):
    """Host urllib.request calls quote/unquote with keyword encoding/errors.

    Stage2 cold-bootstrap failed here twice (2026-09-17): the native
    provider exposed only (s) / (s, safe), and the parallel frontend
    worker rejected the keyword call. The provider must accept the host
    spellings, keep replacement decoding for ill-formed UTF-8, and fail
    closed on unregistered codecs.
    """
    src = tmp_path / "prog.py"
    src.write_text(
        "from urllib.parse import quote, unquote\n"
        "def main():\n"
        "    print(unquote('/a%20b/%C3%A9/%FF/%E2%82x',\n"
        "                  encoding='utf-8', errors='replace'))\n"
        "    print(quote('/a b?c=d'))\n"
        "    print(quote('http://ex.com/a b',\n"
        "                encoding='iso-8859-1',\n"
        "                safe=\"!#$&'()*+,;=?/:@\"))\n"
        "main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "prog"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_GC_BACKEND"] = "4"
    build = subprocess.run(
        [
            "uv", "run", "pcc",
            "--backend", "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src), "-o", str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=420,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [
        "/a b/é/\ufffd/\ufffdx",
        "/a%20b%3Fc%3Dd",
        "http://ex.com/a%20b",
    ]
