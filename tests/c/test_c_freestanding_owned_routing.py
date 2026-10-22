"""Host contracts only: no C/Python frontend compilation or native execution."""
import hashlib
from pathlib import Path
import subprocess

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import elf_x86_64 as elf
from pcc.backend import owned_elf_link
from pcc.backend.ar_writer import write_archive
from pcc.driver.cli_core import cli_main
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.python import owned_runtime_build


def forbidden(*args, **kwargs):
    raise AssertionError('compilation, provisioning or host tool invocation is forbidden in this host test')


def source_file(tmp_path):
    source = tmp_path / 'main.c'
    source.write_text('int main(int argc, char **argv) { return argc; }\n')
    return source


@pytest.mark.parametrize('explicit_system', [False, True])
def test_public_dispatch_preserves_selected_owner(tmp_path, monkeypatch, capsys, explicit_system):
    source = source_file(tmp_path)
    calls = []
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', forbidden)
    monkeypatch.setattr(CEvaluator, 'validate_owned_freestanding_request',
                        lambda self, args: calls.append(('admit', args)))
    def owned(self, units, **kwargs):
        calls.append(('owned', kwargs))
        return subprocess.CompletedProcess([], 23, stdout='owned output\n', stderr='')
    def oracle(self, units, **kwargs):
        calls.append(('oracle', kwargs))
        return subprocess.CompletedProcess([], 31, stdout='oracle output\n', stderr='')
    monkeypatch.setattr(CEvaluator, 'run_translation_units_owned', owned)
    monkeypatch.setattr(CEvaluator, 'run_translation_units_with_system_cc', oracle)
    options = ['--system-link'] if explicit_system else []
    result = cli_main(['--backend=self', '--freestanding-libc'] + options + [str(source), 'arg'])
    assert result == (31 if explicit_system else 23)
    assert [row[0] for row in calls] == (['oracle'] if explicit_system else ['admit', 'owned'])
    assert calls[-1][1]['freestanding_libc'] is True
    assert calls[-1][1]['prog_args'] == ['arg']
    assert capsys.readouterr().out == ('oracle output\n' if explicit_system else 'owned output\n')


def test_output_mode_carries_freestanding_policy_after_early_admission(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    calls = []
    monkeypatch.setattr(CEvaluator, 'validate_owned_freestanding_request',
                        lambda self, args: calls.append('admit'))
    def compile_units(self, *args, **kwargs):
        assert calls == ['admit']
        calls.append('compile')
        return [('sentinel',)]
    def emit(self, units, output, **kwargs):
        assert calls == ['admit', 'compile']
        assert units == [('sentinel',)]
        assert kwargs['freestanding_libc'] is True
        calls.append('owned emit')
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', compile_units)
    monkeypatch.setattr(CEvaluator, 'emit_executable', emit)
    monkeypatch.setattr(CEvaluator, '_system_cc', forbidden)
    assert cli_main(['--backend=self', '--freestanding-libc', '-o', str(tmp_path/'program'), str(source)]) == 0
    assert calls == ['admit', 'compile', 'owned emit']


@pytest.mark.parametrize('option', ['-pie', '-static-pie', '-Wl,--shared'])
def test_invalid_policy_rejected_before_compilation(tmp_path, monkeypatch, capsys, option):
    # The fixed ET_EXEC startup is Linux's freestanding contract; pin the
    # target so the policy is checked on every host, not only on Linux.
    import pcc.frontends.c.evaluator.c_evaluator as c_evaluator
    monkeypatch.setattr(c_evaluator, 'host_target_triple', lambda: 'x86_64-unknown-linux-gnu')
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', forbidden)
    assert cli_main(['--freestanding-libc', '--link-arg='+option, str(source_file(tmp_path))]) == 1
    assert 'fixed ET_EXEC' in capsys.readouterr().err


def test_foreign_archive_rejected_before_compilation(tmp_path, monkeypatch, capsys):
    foreign = tmp_path/'foreign_runtime.a'
    foreign.write_bytes(b'!<arch>\n')
    monkeypatch.setenv('PCC_RUNTIME_ARCHIVE', str(foreign))
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', forbidden)
    assert cli_main(['--freestanding-libc', str(source_file(tmp_path))]) == 1
    assert 'requires libpy_runtime_pcc_py.a' in capsys.readouterr().err


def test_no_provisioning_does_not_create_runtime_or_view(tmp_path, monkeypatch, capsys):
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    monkeypatch.delenv('PCC_RUNTIME_ARCHIVE', raising=False)
    monkeypatch.setenv('PCC_RUNTIME_DIR', str(runtime))
    monkeypatch.setenv('PCC_TEST_NO_NATIVE_PROVISIONING', '1')
    monkeypatch.setattr(owned_runtime_build, 'build_runtime_archive', forbidden)
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', forbidden)
    assert cli_main(['--freestanding-libc', str(source_file(tmp_path))]) == 1
    assert 'automatic native test provisioning is disabled' in capsys.readouterr().err
    assert list(runtime.iterdir()) == []


def test_make_reference_is_not_a_public_owned_runtime(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PCC_RUNTIME_BUILD', 'make')
    monkeypatch.delenv('PCC_RUNTIME_ARCHIVE', raising=False)
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', forbidden)
    assert cli_main(['--freestanding-libc', str(source_file(tmp_path))]) == 1
    assert 'requires the owned runtime builder' in capsys.readouterr().err


@pytest.mark.parametrize('config', [None, {}, {'threads': False, 'refcount': 'atomic'},
                                   {'threads': True, 'refcount': 'plain'}])
def test_archive_members_never_inherit_missing_or_mismatched_config(monkeypatch, config):
    monkeypatch.setattr(owned_runtime_build, 'runtime_modules', lambda *args: ['one'])
    member = {'member': 'one.o'}
    if config is not None:
        member['runtime_build_config'] = config
    receipt = {'target_triple': 'x86_64-unknown-linux-gnu', 'members': [member]}
    assert not owned_runtime_build._manifest_matches_config(
        receipt, '.', 'x86_64-unknown-linux-gnu', {'threads': True, 'refcount': 'atomic'})


def elf_object(defined=(), undefined=()):
    symbols = [elf.ElfSymbol.null()]
    symbols.extend(elf.ElfSymbol(name, 1, 0, 1, elf.STB_GLOBAL, elf.STT_FUNC) for name in defined)
    symbols.extend(elf.ElfSymbol(name, elf.SHN_UNDEF, 0, 0, elf.STB_GLOBAL, elf.STT_NOTYPE) for name in undefined)
    return elf.ElfObject((elf.ElfSection('.text', elf.SHT_PROGBITS,
                         elf.SHF_ALLOC | elf.SHF_EXECINSTR, 1, b'\xc3'),), tuple(symbols))


def test_owned_map_uses_actual_selected_members_and_byte_hashes(tmp_path, monkeypatch):
    # Construct tiny ELF records directly. No source compilation or execution.
    root = tmp_path/'input.o'
    root.write_bytes(elf.emit_relocatable(elf_object(('_start',), ('wanted',))))
    wanted = elf.emit_relocatable(elf_object(('wanted',)))
    unused = elf.emit_relocatable(elf_object(('unused',)))
    archive = tmp_path/'libpy_runtime_pcc_py.a'
    archive.write_bytes(write_archive([('wanted.o', wanted), ('unused.o', unused)]))
    monkeypatch.setattr(owned_elf_link, '_thread_pointer_object', lambda target: elf_object())
    monkeypatch.setattr(owned_elf_link, 'assemble', lambda text, target: elf_object())
    output, link_map = tmp_path/'program', tmp_path/'owned.map'
    owned_elf_link.link_inputs(target='x86_64-unknown-linux-gnu', output=str(output),
                              objects=[str(root)], archives=[str(archive)], map_path=str(link_map))
    text = link_map.read_text()
    assert str(archive)+'(wanted.o)' in text
    assert 'unused.o' not in text
    assert 'libpcc_freestanding_c.a' not in text
    assert 'member_sha256='+hashlib.sha256(wanted).hexdigest() in text
    assert 'archive_sha256='+hashlib.sha256(archive.read_bytes()).hexdigest() in text
    assert 'image sha256='+hashlib.sha256(output.read_bytes()).hexdigest() in text
    assert elf.parse_static_executable(output.read_bytes())['entry'] > 0


@pytest.mark.parametrize('alias', ['output', 'object', 'archive'])
def test_owned_map_cannot_overwrite_inputs(tmp_path, alias):
    output, obj, archive = (tmp_path/name for name in ('program', 'input.o', 'runtime.a'))
    obj.write_bytes(b'protected object')
    archive.write_bytes(b'protected archive')
    paths = {'output': output, 'object': obj, 'archive': archive}
    with pytest.raises(elf.ElfError, match='aliases'):
        owned_elf_link.link_inputs(target='x86_64-unknown-linux-gnu', output=str(output),
                                  objects=[str(obj)], archives=[str(archive)], map_path=str(paths[alias]))
    assert obj.read_bytes() == b'protected object'
    assert archive.read_bytes() == b'protected archive'


def test_owned_run_uses_emitter_and_only_runs_result(tmp_path, monkeypatch):
    evaluator = CEvaluator()
    calls = []
    monkeypatch.setattr(evaluator, '_system_cc', forbidden)
    monkeypatch.setattr(evaluator, '_self_backend_asm_text', forbidden)
    monkeypatch.setattr(evaluator, 'emit_executable',
                        lambda units, output, **kw: calls.append(('emit', output, kw)))
    def run(command, **kwargs):
        calls.append(('run', command, kwargs))
        return subprocess.CompletedProcess(command, 7, stdout='ok', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    result = evaluator._run_compiled_translation_units_self_backend(
        [], optimize=False, freestanding_libc=True, prog_args=['a'], capture_output=True, text=True)
    assert result.returncode == 7
    assert [row[0] for row in calls] == ['emit', 'run']
    assert calls[0][2]['freestanding_libc'] is True
    assert calls[1][1] == [calls[0][1], 'a']


def test_canonical_archive_name_does_not_bypass_provenance(tmp_path, monkeypatch, capsys):
    archive = tmp_path/'libpy_runtime_pcc_py.a'
    archive.write_bytes(b'!<arch>\n')
    monkeypatch.setenv('PCC_RUNTIME_ARCHIVE', str(archive))
    monkeypatch.setenv('PCC_TEST_NO_NATIVE_PROVISIONING', '1')
    monkeypatch.setattr(owned_runtime_build, 'build_runtime_archive', forbidden)
    monkeypatch.setattr(CEvaluator, 'compile_translation_units', forbidden)
    assert cli_main(['--freestanding-libc', str(source_file(tmp_path))]) == 1
    assert 'provenance' in capsys.readouterr().err
    assert archive.read_bytes() == b'!<arch>\n'


def test_owned_map_temporary_cannot_overwrite_archive(tmp_path):
    archive = tmp_path/'owned.map.pcc-link.tmp'
    archive.write_bytes(b'protected archive')
    with pytest.raises(elf.ElfError, match='aliases'):
        owned_elf_link.link_inputs(target='x86_64-unknown-linux-gnu', output=str(tmp_path/'program'),
                                  archives=[str(archive)], map_path=str(tmp_path/'owned.map'))
    assert archive.read_bytes() == b'protected archive'



def macho_sections(name, calls=None):
    import struct
    from pcc.backend import macho_obj, macho_spec
    words = [0x94000000, 0xD65F03C0] if calls else [0xD65F03C0]
    relocations = (() if calls is None else (
        macho_obj.Relocation(0, calls, macho_spec.ARM64_RELOC_BRANCH26, True),))
    section = macho_obj.Section('__text', '__TEXT', struct.pack('<'+'I'*len(words), *words),
                               2, macho_obj.TEXT_SECTION_FLAGS,
                               (macho_obj.TextSymbol(name, 0),), relocations)
    return [section], ([] if calls is None else [calls])


def macho_object(name, calls=None):
    from pcc.backend.native_object import NativeObject
    sections, undefined = macho_sections(name, calls)
    return NativeObject.from_sections(sections, undefined=undefined)


def test_macho_receipt_records_actual_members_and_declared_imports(monkeypatch):
    from pcc.backend import macho_exec, macho_spec
    wanted = macho_object('_wanted', '_puts').to_macho()
    unused = macho_object('_unused').to_macho()
    archive = write_archive([('wanted.o', wanted), ('unused.o', unused)])
    observed = []
    def controlled_export_oracle(name):
        observed.append(name)
        return name == '_puts'
    monkeypatch.setattr(macho_exec, '_libsystem_exports_symbol', controlled_export_oracle)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    receipt = {'old': 'must be replaced only on success'}
    image = macho_exec.link_executable([macho_object('_main', '_wanted')],
                                      archives=[archive], link_receipt=receipt)
    assert receipt['archive_members'] == [{
        'archive_index': 0, 'member': 'wanted.o',
        'archive_sha256': hashlib.sha256(archive).hexdigest(),
        'member_sha256': hashlib.sha256(wanted).hexdigest(),
    }]
    assert receipt['dynamic_libraries'] == [{'ordinal': 1, 'install_name': '/usr/lib/libSystem.B.dylib'}]
    assert receipt['imports'] == [{'symbol': '_puts', 'library_ordinal': 1, 'weak': False}]
    assert receipt['image_sha256'] == hashlib.sha256(image).hexdigest()
    assert observed == ['_puts']
    assert 'old' not in receipt
    import struct
    parsed = macho_spec.parse_object(image)
    declared = [command for command in parsed.commands if command.cmd == macho_spec.LC_LOAD_DYLIB]
    assert len(declared) == 1
    name_offset, = struct.unpack_from('<I', declared[0].raw, 8)
    assert declared[0].raw[name_offset:].split(b'\0', 1)[0] == b'/usr/lib/libSystem.B.dylib'
    imports = [row for row in parsed.symbols() if (row['n_type'] & macho_spec.N_TYPE) == macho_spec.N_UNDF]
    assert [(row['name'], (row['n_desc'] >> 8) & 255) for row in imports] == [('_puts', 1)]
    # The optional receipt does not change the produced image.
    assert image == macho_exec.link_executable([macho_object('_main', '_wanted')], archives=[archive])


def test_failed_macho_import_admission_does_not_publish_receipt(monkeypatch):
    from pcc.backend import macho_exec
    from pcc.backend.macho_link import LinkError
    monkeypatch.setattr(macho_exec, '_libsystem_exports_symbol', lambda name: False)
    receipt = {'sentinel': 'retained'}
    with pytest.raises(LinkError, match='not exported'):
        macho_exec.link_executable([macho_object('_main', '_missing')], link_receipt=receipt)
    assert receipt == {'sentinel': 'retained'}


def test_darwin_emitter_map_has_real_hashes_without_sdk_owner_claims(tmp_path, monkeypatch):
    import json
    from pcc.backend import arm64_asm_driver, macho_exec
    evaluator = CEvaluator(target_triple='arm64-apple-darwin')
    wanted = macho_object('_wanted', '_puts').to_macho()
    archive = tmp_path/'libpy_runtime_pcc_py.a'
    archive.write_bytes(write_archive([('wanted.o', wanted)]))
    monkeypatch.setattr(evaluator, '_system_cc', forbidden)
    monkeypatch.setattr(evaluator, '_self_link_target_identity', lambda units: 'self-aarch64-darwin-v0')
    monkeypatch.setattr(evaluator, '_self_backend_asm_text', lambda units: 'host fixture; no assembly parsing')
    monkeypatch.setattr(arm64_asm_driver, 'assemble_file', lambda text: macho_sections('_main', '_wanted'))
    monkeypatch.setattr(macho_exec, '_libsystem_exports_symbol', lambda name: name == '_puts')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    output, link_map = tmp_path/'program', tmp_path/'darwin.map'
    evaluator.emit_executable([('host model fixture',)], str(output), optimize=False,
                              link_args=[str(archive), '-Wl,-map,'+str(link_map)])
    text = link_map.read_text()
    assert str(archive)+'(wanted.o)' in text
    assert 'member_sha256='+hashlib.sha256(wanted).hexdigest() in text
    assert 'archive_sha256='+hashlib.sha256(archive.read_bytes()).hexdigest() in text
    assert 'image sha256='+hashlib.sha256(output.read_bytes()).hexdigest() in text
    libraries = [json.loads(line[len('# declared_library '):]) for line in text.splitlines()
                 if line.startswith('# declared_library ')]
    imports = [json.loads(line[len('# dynamic_import '):]) for line in text.splitlines()
               if line.startswith('# dynamic_import ')]
    assert libraries == [{'ordinal': 1, 'install_name': '/usr/lib/libSystem.B.dylib'}]
    assert imports == [{'symbol': '_puts', 'library_ordinal': 1, 'weak': False}]
    assert '.tbd' not in text
    assert 'physical reexport owners are not established' in text


def test_macho_receipt_retains_weak_import_without_claiming_export_admission(monkeypatch):
    from pcc.backend import macho_exec
    from pcc.backend.native_object import NativeObject
    sections, undefined = macho_sections('_main', '_optional')
    obj = NativeObject.from_sections(sections, undefined=undefined, weak_undefined=('_optional',))
    monkeypatch.setattr(macho_exec, '_libsystem_exports_symbol', forbidden)
    receipt = {}
    macho_exec.link_executable([obj], link_receipt=receipt)
    assert receipt['imports'] == [{'symbol': '_optional', 'library_ordinal': 1, 'weak': True}]


def test_macho_selection_receipt_keeps_repeated_scan_order(monkeypatch):
    from pcc.backend import macho_exec
    early = macho_object('_early').to_macho()
    late = macho_object('_late', '_early').to_macho()
    unused = macho_object('_unused').to_macho()
    archive = write_archive([('early.o', early), ('late.o', late), ('unused.o', unused)])
    monkeypatch.setattr(macho_exec, '_libsystem_exports_symbol', forbidden)
    receipt = {}
    macho_exec.link_executable([macho_object('_main', '_late')], archives=[archive], link_receipt=receipt)
    assert [row['member'] for row in receipt['archive_members']] == ['early.o', 'late.o']
    assert [row['member_sha256'] for row in receipt['archive_members']] == [
        hashlib.sha256(early).hexdigest(), hashlib.sha256(late).hexdigest()]
    assert receipt['imports'] == []
