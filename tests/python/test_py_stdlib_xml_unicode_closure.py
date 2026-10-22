"""Focused source for Meson's Unicode-width and ElementTree closure."""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata as host_unicodedata
import xml.etree.ElementTree as host_etree

import pytest

from pcc.stdlib import unicodedata as port_unicodedata
from pcc.stdlib.xml.etree import ElementTree as port_etree


@pytest.mark.parametrize(
    "character",
    ["A", "\u4e2d", "\u1100", "\uff21", "\uff71", "\U0001f642"],
)
def test_east_asian_width_matches_cpython_for_build_tool_classes(character):
    assert port_unicodedata.east_asian_width(character) == (
        host_unicodedata.east_asian_width(character)
    )


def _tree(module):
    root = module.Element("testsuites", tests="1", errors="0")
    suite = module.SubElement(root, "testsuite", {"name": "native & owned"})
    case = module.SubElement(suite, "testcase", name="case-1")
    module.SubElement(case, "system-out").text = "left < right"
    module.SubElement(case, "skipped")
    return root


def test_element_construction_and_serialization_match_cpython():
    assert port_etree.tostring(_tree(port_etree), encoding="unicode") == (
        host_etree.tostring(_tree(host_etree), encoding="unicode")
    )
    assert port_etree.tostring(
        _tree(port_etree), encoding="utf-8", xml_declaration=True
    ) == host_etree.tostring(
        _tree(host_etree), encoding="utf-8", xml_declaration=True
    )


def test_element_parse_text_tail_and_xpath_subset_match_cpython():
    source = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<testsuites><!--comment--><testsuite name='one'>"
        "prefix<testcase result='ok'>value &amp; more</testcase>tail"
        "<testcase timestamp='now' /></testsuite></testsuites>"
    )
    port_root = port_etree.fromstring(source)
    host_root = host_etree.fromstring(source)
    assert port_root.tag == host_root.tag
    assert [item.tag for item in port_root.findall(".")] == [
        item.tag for item in host_root.findall(".")
    ]
    assert [item.attrib for item in port_root.findall(".//testcase")] == [
        item.attrib for item in host_root.findall(".//testcase")
    ]
    assert [item.text for item in port_root.findall(".//testcase[@result]")] == [
        item.text for item in host_root.findall(".//testcase[@result]")
    ]
    assert port_root.find(".//testcase").tail == host_root.find(
        ".//testcase"
    ).tail


def test_element_tree_file_roundtrip_matches_cpython(tmp_path):
    port_path = tmp_path / "port.xml"
    host_path = tmp_path / "host.xml"
    port_etree.ElementTree(_tree(port_etree)).write(
        port_path, encoding="utf-8", xml_declaration=True
    )
    host_etree.ElementTree(_tree(host_etree)).write(
        host_path, encoding="utf-8", xml_declaration=True
    )
    assert port_path.read_bytes() == host_path.read_bytes()
    reparsed = port_etree.parse(port_path)
    assert reparsed.getroot().find(".//testcase").attrib == {"name": "case-1"}


def test_unowned_xml_surfaces_fail_closed():
    with pytest.raises(port_etree.ParseError, match="DTD"):
        port_etree.fromstring("<!DOCTYPE root><root />")
    with pytest.raises(NotImplementedError, match="XPath"):
        port_etree.Element("root").findall(".//item[1]")
    with pytest.raises(NotImplementedError, match="encoding"):
        port_etree.tostring(port_etree.Element("root"), encoding="utf-16")


@pytest.mark.parametrize(
    "module_name,suffix",
    [
        ("unicodedata", "/pcc/stdlib/unicodedata.py"),
        ("xml", "/pcc/stdlib/xml/__init__.py"),
        ("xml.etree", "/pcc/stdlib/xml/etree/__init__.py"),
        ("xml.etree.ElementTree", "/pcc/stdlib/xml/etree/ElementTree.py"),
    ],
)
def test_xml_unicode_family_is_selected_by_recursive_stdlib_registry(
    module_name, suffix
):
    from pcc.frontends.python import pipeline

    source = pipeline._locate_native_stdlib_module_source(module_name)
    assert source is not None
    assert source.endswith(suffix)
    assert pipeline._classify_python_import(module_name) == "native_stdlib"


@pytest.mark.integration
def test_xml_unicode_match_cpython_strict_self_no_libpython(tmp_path):
    source = '''\
import unicodedata
import xml.etree.ElementTree as ET

root = ET.Element("testsuites", tests="1", errors="0")
suite = ET.SubElement(root, "testsuite", name="native & owned")
case = ET.SubElement(suite, "testcase", result="ok")
case.text = "left < right"
xml = ET.tostring(root, encoding="unicode")
parsed = ET.fromstring(xml)
print("width", [unicodedata.east_asian_width(c) for c in "A\u4e2d\uff21\uff71\U0001f642"])
print("xml", xml)
print("find", parsed.find(".//testcase[@result]").text)
'''
    src = tmp_path / "xml_unicode_probe.py"
    src.write_text(source, encoding="utf-8")
    executable = tmp_path / "xml_unicode_probe"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_RUNTIME_CC", None)
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
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=900,
        env=env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert "PCC-PY-COMPILE-001" not in build.stdout + build.stderr

    no_host_env = env.copy()
    no_host_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    no_host_env["PATH"] = str(tmp_path / "no-host-python")
    actual = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=120,
        env=no_host_env,
    )
    expected = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert actual.returncode == 0, actual.stdout + actual.stderr
    assert expected.returncode == 0, expected.stdout + expected.stderr
    assert actual.stdout == expected.stdout


# HTML belongs to the same owned markup family. These are host/oracle controls;
# native execution is a separate integration gate below.
from html.parser import HTMLParser as HostHTMLParser
from pcc.stdlib.html.parser import HTMLParser as OwnedHTMLParser
from pcc.stdlib import html as owned_html
import html as host_html
import random

def _html_collect(parser_type, chunks, convert=True, scripting=False):
    class Collector(parser_type):
        def __init__(self):
            self.events = []
            super().__init__(convert_charrefs=convert, scripting=scripting)

        def handle_starttag(self, tag, attrs):
            self.events.append(('start', tag, attrs, self.get_starttag_text(), self.getpos()))

        def handle_endtag(self, tag):
            self.events.append(('end', tag, self.getpos()))

        def handle_data(self, data):
            self.events.append(('data', data, self.getpos()))

        def handle_comment(self, data):
            self.events.append(('comment', data, self.getpos()))

        def handle_decl(self, data):
            self.events.append(('decl', data, self.getpos()))

        def handle_pi(self, data):
            self.events.append(('pi', data, self.getpos()))

        def handle_charref(self, name):
            self.events.append(('charref', name, self.getpos()))

        def handle_entityref(self, name):
            self.events.append(('entityref', name, self.getpos()))

        def unknown_decl(self, data):
            self.events.append(('unknown', data, self.getpos()))

    parser = Collector()
    for chunk in chunks:
        parser.feed(chunk)
    parser.close()
    return parser.events, parser.getpos(), parser.rawdata


_HTML_CORPUS = [
    '<A HREF="a&amp;b" data-requires-python=">=3.10">one</A>',
    "text\n<a X=1 x='&notit;' y='&copy=1' z='&copy;=1' disabled />tail",
    '<!DOCTYPE html><!-- hi --!><?process x?><![CDATA[x<y&z]]><!bogus>',
    '<!--><!---><!--x--><!--x--!><!--nested<!--x-->tail',
    '<script>a < b &amp; </scripty> q</SCRIPT>tail',
    '<style>x</style><xmp>y &lt;<p></xmp>',
    '<title>a &amp; <b></title><textarea>&notit;<a></textarea>',
    '<plaintext>&amp;<b>text</plaintext>',
    '<noscript><b>x &amp;</b></noscript>',
    'a\n&#0; &#x80; &#xD800; &#x110000; &#11; &NotEqualTilde; &unknown;',
    '<x a=1/b c = "q>r"d=2 // a==b a=/>',
    '</> </!bad> <3 <!> </a x=">"/>',
    '<a unfinished="', '<!--partial--!', '<?partial', '<!DOCTYPE partial',
    '<![CDATA[partial', '&#12', '&#12a;', '&#x4f', '&word', '&word.', '&',
    '<', '</', '</a', '</?', '<!partial',
]



@pytest.mark.parametrize('source', _HTML_CORPUS)
@pytest.mark.parametrize('convert', [True, False])
@pytest.mark.parametrize('scripting', [True, False])
def test_html_streaming_matches_cpython(source, convert, scripting):
    chunkings = [[source], list(source)]
    chunkings += [[source[:i], source[i:]] for i in range(len(source) + 1)]
    for chunks in chunkings:
        assert _html_collect(OwnedHTMLParser, chunks, convert, scripting) == (
            _html_collect(HostHTMLParser, chunks, convert, scripting)
        ), chunks


def test_html_randomized_tolerant_markup_matches_cpython():
    rng = random.Random(31315)
    atoms = ['<', '>', '/', 'a', 'B', 'x', ' ', '\n', '\t', '=', "'", '"',
             '&', ';', '#', '!--', '!', '?', 'doctype', 'script', '-', '.',
             '12', 'af', 'ü', '\x00']
    for _ in range(6000):
        source = ''.join(rng.choice(atoms) for _ in range(rng.randrange(1, 24)))
        chunks = [source] if rng.randrange(2) else list(source)
        convert = bool(rng.randrange(2))
        assert _html_collect(OwnedHTMLParser, chunks, convert) == (
            _html_collect(HostHTMLParser, chunks, convert)
        ), chunks
        assert owned_html.unescape(source) == host_html.unescape(source)


def test_html_entity_table_and_all_entity_spellings_match_cpython():
    from pcc.stdlib.html import entities as owned_entities
    assert owned_entities.html5 == host_html.entities.html5
    assert owned_entities.name2codepoint == host_html.entities.name2codepoint
    assert owned_entities.codepoint2name == host_html.entities.codepoint2name
    assert owned_entities.entitydefs == host_html.entities.entitydefs
    for key in host_html.entities.html5:
        for suffix in ('', ';', 'x', '=', ' '):
            text = '&' + key + suffix
            assert owned_html.unescape(text) == host_html.unescape(text), text


def test_html_escape_and_numeric_references_match_cpython():
    for quote in (True, False):
        for text in ('', '<&>\'"', 'é\U0001f642', '&amp;'):
            assert owned_html.escape(text, quote=quote) == host_html.escape(text, quote=quote)
    numbers = list(range(256)) + [
        0xD7FF, 0xD800, 0xDFFF, 0xE000, 0xFDD0, 0xFDEF, 0xFFFE,
        0xFFFF, 0x10000, 0x1F642, 0x10FFFF, 0x110000, 10**30,
    ]
    for number in numbers:
        for text in ('&#' + str(number), '&#x' + format(number, 'x')):
            for suffix in ('', ';', '=x'):
                assert owned_html.unescape(text + suffix) == host_html.unescape(text + suffix)


def test_html_custom_raw_text_end_tag_case_matching_is_ascii_only():
    for parser_type in (OwnedHTMLParser, HostHTMLParser):
        class Recorder(parser_type):
            CDATA_CONTENT_ELEMENTS = ('link',)
            def __init__(self):
                super().__init__()
                self.events = []
            def handle_starttag(self, tag, attrs):
                self.events.append(('start', tag))
            def handle_endtag(self, tag):
                self.events.append(('end', tag))
            def handle_data(self, data):
                self.events.append(('data', data))
        parser = Recorder()
        parser.feed('<link>inside</linK>outside')
        parser.close()
        assert parser.events == [('start', 'link'), ('data', 'inside</linK>outside')]
        assert parser.cdata_elem == 'link'
        parser.feed('</LINK>')
        parser.close()
        assert parser.events[-1] == ('end', 'link')
        assert parser.cdata_elem is None


def test_html_reset_close_and_cdata_switch_match_cpython():
    for parser_type in (OwnedHTMLParser, HostHTMLParser):
        class Recorder(parser_type):
            def __init__(self):
                super().__init__()
                self.events = []
            def handle_data(self, data):
                self.events.append(('data', data, self.getpos()))
            def handle_comment(self, data):
                self.events.append(('comment', data, self.getpos()))
        parser = Recorder()
        assert parser.getpos() == (1, 0)
        assert parser.get_starttag_text() is None
        parser.feed('<a x="partial')
        parser.reset()
        parser.feed('fresh\n&amp')
        parser.close()
        assert parser.events == [('data', 'fresh\n&', (1, 0))]
        assert parser.getpos() == (2, 4)
        parser.reset()
        parser._set_support_cdata(False)
        parser.feed('<![CDATA[x]]>')
        parser.close()
        assert parser.events[-1] == ('comment', '[CDATA[x]]', (1, 0))


def test_html_original_simple_links_class_with_owned_provider():
    # Execute the unchanged original class against the owned provider. This is
    # a host causal control, not proof of native imported-subclass execution.
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / 'pcc/package/acquire.py'
    source = path.read_text(encoding='utf-8')
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.ClassDef) and node.name == '_SimpleLinks')
    namespace = {'HTMLParser': OwnedHTMLParser}
    exec(ast.get_source_segment(source, node), namespace)
    parser = namespace['_SimpleLinks']()
    parser.feed('<A HREF="pkg.whl?x=1&amp;y=2" data-requires-python=">=3.10">')
    parser.feed('<script><a href="ignored"></script><a href=other.whl/>')
    parser.close()
    assert parser.hrefs == ['pkg.whl?x=1&y=2', 'other.whl/']
    assert parser.links == [('pkg.whl?x=1&y=2', '>=3.10'), ('other.whl/', '')]


@pytest.mark.parametrize('module_name, suffix', [
    ('html', '/pcc/stdlib/html/__init__.py'),
    ('html.parser', '/pcc/stdlib/html/parser.py'),
    ('html.entities', '/pcc/stdlib/html/entities.py'),
])
def test_html_family_is_selected_by_recursive_stdlib_registry(module_name, suffix):
    from pcc.frontends.python import pipeline
    provider = pipeline._locate_native_stdlib_module_source(module_name)
    assert provider is not None and provider.endswith(suffix), provider
    assert pipeline._classify_python_import(module_name) == 'native_stdlib'


@pytest.fixture
def html_package_source(monkeypatch):
    """Give the compiler and reference the same explicit package source."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for variable in ("PCC_PACKAGE_SITE", "PYTHONPATH"):
        previous = os.environ.get(variable, "")
        value = str(root) + (os.pathsep + previous if previous else "")
        monkeypatch.setenv(variable, value)
    return root


def test_original_html_package_fixture_is_in_native_source_closure(
    tmp_path, html_package_source,
):
    from pcc.frontends.python.pipeline import _collect_relative_module_closure
    fixture = html_package_source / "tests/fixtures/html_parser_package_acquire.py"
    source = tmp_path / fixture.name
    source.write_bytes(fixture.read_bytes())
    sources, modules = _collect_relative_module_closure(str(source))
    actual = dict(zip(modules, sources))
    assert actual["pcc.package.acquire"] == str(
        html_package_source / "pcc/package/acquire.py"
    )
    assert source.read_bytes() == fixture.read_bytes()


@pytest.mark.integration
@pytest.mark.parametrize('fixture_name', [
    'html_parser_streaming.py',
    'html_parser_package_acquire.py',
])
def test_html_owned_native_subclass_execution(
    fixture_name, tmp_path, monkeypatch, pcc_runtime_archive, html_package_source,
):
    """Execute generic and original imported subclasses under every GC.

    This is the native acceptance gate. Host differentials and IR emission
    cannot satisfy it, and an unavailable runtime must never count as a pass.
    """
    import hashlib
    import json
    from pathlib import Path
    from pcc.frontends.python.pipeline import compile_python
    from pcc.diagnostics.gc_log import parse_log_lines

    fixture = Path(__file__).resolve().parents[1] / 'fixtures' / fixture_name
    source = tmp_path / fixture_name
    source.write_bytes(fixture.read_bytes())
    executable = source.with_suffix('')
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    monkeypatch.delenv('LC_ALL', raising=False)
    monkeypatch.setenv('PCC_HOST_PYTHON', sys.executable)
    compile_python(str(source), str(executable), backend='self',
                   libpython_mode='off', ir_scaffold_mode='on',
                   runtime_archive=str(pcc_runtime_archive))
    binary_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    receipts = []
    for collector in range(5):
        log = tmp_path / ('html-gc' + str(collector) + '.jsonl')
        environment = dict(os.environ, PCC_GC_BACKEND=str(collector),
                           PCC_LOG='gc', PCC_LOG_FORMAT='json', PCC_LOG_FILE=str(log),
                           PCC_HOST_PYTHON='/unavailable/host-python',
                           PATH=str(tmp_path / 'no-host-tools'))
        actual = subprocess.run([str(executable)], capture_output=True, text=True,
                                timeout=30, env=environment)
        assert actual.returncode == 0, actual.stderr
        assert actual.stdout == reference.stdout
        assert actual.stderr == reference.stderr
        assert log.is_file(), 'collector selection requires an observed witness'
        events = parse_log_lines(log.read_text(encoding='utf-8').splitlines())
        observed = sorted({event.fields['value1'] for event in events
                           if event.fields.get('category') == 'gc'
                           and event.event in ('collect_start', 'collect_stop', 'collect_end')})
        receipts.append({'fixture': fixture_name, 'requested_backend': collector,
                         'observed_backends': observed, 'binary_sha256': binary_hash})
        (tmp_path / 'html-native-receipts.json').write_text(
            json.dumps(receipts, indent=2) + '\n', encoding='utf-8',
        )
        assert observed == [collector], receipts[-1]
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == binary_hash
