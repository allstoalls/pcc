"""Stdlib providers print what CPython 3.15.0 prints.

The oracle moved from 3.15.0rc1 to 3.15.0, which changed netrc (an empty
quoted token is a token, not end of input: gh-155468) and configparser
(whitespace delimiters: gh-156353). The same program also pins provider
behavior that never matched CPython: read_string split lines at every
str.splitlines boundary instead of only "\\n", read_file required .read(),
sysconfig listed Homebrew's osx_framework_library scheme and reported the
base prefix as ``prefix``, and native sys.prefix asked a host python3.

EXPECTED is CPython 3.15.0's output. The providers must print it on the
host, and the native binary must print it under every collector with no
host Python reachable.
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys


PROGRAM = r'''
import configparser
import netrc
import os
import sys
import sysconfig


def netrc_case(directory, index, text):
    path = os.path.join(directory, "case" + str(index) + ".netrc")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)
    try:
        parsed = netrc.netrc(path)
    except netrc.NetrcParseError as error:
        return "error " + error.msg
    hosts = []
    for name in sorted(parsed.hosts):
        hosts.append(name + "=" + repr(parsed.hosts[name]))
    macros = []
    for name in sorted(parsed.macros):
        macros.append(name + "=" + repr(parsed.macros[name]))
    return "hosts [" + ", ".join(hosts) + "] macros [" + ", ".join(macros) + "]"


def config_case(delimiters, text):
    parser = configparser.ConfigParser(
        delimiters=delimiters, inline_comment_prefixes=None
    )
    parser.read_string(text)
    pairs = []
    for section in parser.sections():
        for option in parser.options(section):
            pairs.append(section + "." + option + "=" + repr(parser.get(section, option)))
    return " ".join(pairs)


def main():
    directory = sys.argv[1]
    netrc_texts = [
        'machine host.domain.com login ""\n',
        'machine host.domain.com account ""\n',
        'machine host.domain.com password ""\n',
        'default login ""\n',
        'machine host.domain.com login "" password\n',
        '"" invalid\n',
        'machine host.domain.com "" invalid\n',
        "#comment machine near login kept\n",
        "# machine far login skipped\nmachine next login seen\n",
        "macdef",
    ]
    index = 0
    for text in netrc_texts:
        print("netrc", index, netrc_case(directory, index, text))
        index += 1

    for data in ("foo bar=baz", "foo   bar=baz", "foo=bar=baz", "foo = bar=baz",
                 "foo\t \t=\t \tbar=baz"):
        print("space-delimiter", repr(data), config_case((" ", "="), "[all]\n" + data))
    mismatches = 0
    total = 0
    for delimiter in " =:;#x\t\0\N{RS}\N{CEDILLA}\N{CAT}":
        for before in ("", " ", "\t", " \t"):
            for after in ("", " ", "\t", " \t"):
                got = config_case(
                    (delimiter,), "[all]\nfoo" + before + delimiter + after + "bar=baz"
                )
                total += 1
                if got != "all.foo='bar=baz'":
                    mismatches += 1
                    print("any-delimiter mismatch", repr(delimiter), repr(before),
                          repr(after), got)
    print("any-delimiter", total, mismatches)
    print("record-separator", config_case(("=",), "[s]\nkey = a\x1eb\n"))
    listed = configparser.ConfigParser()
    listed.read_file(["[s]\n", "k = v\n", "other = w"])
    print("read-file-list", repr(listed.get("s", "k")), repr(listed.get("s", "other")))
    disk = os.path.join(directory, "disk.ini")
    with open(disk, "w", encoding="utf-8") as stream:
        stream.write("[disk]\r\nvalue = crlf\r\nother = two\n")
    from_disk = configparser.ConfigParser()
    print("read-disk", from_disk.read(disk, encoding="utf-8") == [disk],
          repr(from_disk.get("disk", "value")), repr(from_disk.get("disk", "other")))

    print("schemes", sysconfig.get_scheme_names())
    print("prefix-is-sys-prefix",
          sysconfig.get_config_var("prefix") == os.path.normpath(sys.prefix))


main()
'''

EXPECTED = """\
netrc 0 hosts [host.domain.com=('', '', '')] macros []
netrc 1 hosts [host.domain.com=('', '', '')] macros []
netrc 2 hosts [host.domain.com=('', '', '')] macros []
netrc 3 hosts [default=('', '', '')] macros []
netrc 4 hosts [host.domain.com=('', '', '')] macros []
netrc 5 error bad toplevel token ''
netrc 6 error bad follower token ''
netrc 7 hosts [near=('kept', '', '')] macros []
netrc 8 hosts [next=('seen', '', '')] macros []
netrc 9 error Macro definition missing null line terminator.
space-delimiter 'foo bar=baz' all.foo='bar=baz'
space-delimiter 'foo   bar=baz' all.foo='bar=baz'
space-delimiter 'foo=bar=baz' all.foo='bar=baz'
space-delimiter 'foo = bar=baz' all.foo='bar=baz'
space-delimiter 'foo\\t \\t=\\t \\tbar=baz' all.foo='bar=baz'
any-delimiter 176 0
record-separator s.key='a\\x1eb'
read-file-list 'v' 'w'
read-disk True 'crlf' 'two'
schemes ('nt', 'nt_user', 'nt_venv', 'osx_framework_user', 'posix_home', 'posix_prefix', 'posix_user', 'posix_venv', 'venv')
prefix-is-sys-prefix True
"""


def test_providers_print_the_cpython_3150_output(tmp_path, monkeypatch, capsys):
    from pcc.stdlib import configparser as port_configparser
    from pcc.stdlib import netrc as port_netrc
    from pcc.stdlib import sysconfig as port_sysconfig

    source = tmp_path / "oracle_deltas.py"
    source.write_text(PROGRAM)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setitem(sys.modules, "configparser", port_configparser)
    monkeypatch.setitem(sys.modules, "netrc", port_netrc)
    monkeypatch.setitem(sys.modules, "sysconfig", port_sysconfig)
    monkeypatch.setattr(sys, "argv", [str(source), str(work)])
    runpy.run_path(str(source), run_name="__main__")
    assert capsys.readouterr().out == EXPECTED


def test_native_providers_print_the_cpython_3150_output(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "oracle_deltas.py"
    output = tmp_path / "oracle_deltas"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", recursive_stdlib=True,
                   runtime_archive=str(pcc_runtime_archive))
    work = tmp_path / "work"
    work.mkdir()
    for gc in range(5):
        result = subprocess.run(
            [str(output), str(work)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="",
                     PCC_HOST_PYTHON="/nonexistent/host-python"),
        )
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == EXPECTED, f"GC{gc}: " + result.stdout
