"""Final-link regressions; no external compiler, assembler, or linker."""
from dataclasses import replace
from pathlib import Path
import os
import struct
import subprocess
import sys

import pytest
from pcc.backend import macho_exec, macho_spec as spec
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_link import LinkError, link_relocatable_native, link_relocatable
from pcc.backend.native_object import NativeObject, encode_native_object, decode_native_object, decode_packed_native_object
from pcc.backend.ar_writer import write_archive
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator


def _object(name='_missing', weak=False):
    sections, undefined = assemble_file(
        '.section __TEXT,__text,regular,pure_instructions\n'
        '.globl _main\n_main:\n stp x29, x30, [sp, #-16]!\n'
        ' mov x29, sp\n bl '+name+'\n ldp x29, x30, [sp], #16\n ret\n')
    return NativeObject.from_sections(sections, undefined=undefined,
                                     weak_undefined=(name,) if weak else ())


def _imports(image):
    obj=spec.parse_object(image)
    command=next(c for c in obj.commands if c.cmd==0x80000034)
    _,_,offset,size=struct.unpack_from('<4I',command.raw)
    fixups=image[offset:offset+size]
    _,_,at,names,count,_,_=struct.unpack_from('<7I',fixups)
    result={}
    for i in range(count):
        word,=struct.unpack_from('<I',fixups,at+i*4)
        name=fixups[names+(word>>9):].split(b'\0',1)[0].decode()
        result[name]=(word&255,bool(word&256))
    return result


def test_body1_final_link_fails_without_publishing(tmp_path, monkeypatch):
    source='int f(void);\nint main(void) { return f(); }\n'
    evaluator=CEvaluator(target_triple='arm64-apple-darwin',backend='self')
    units=evaluator.compile_translation_units(
        [TranslationUnit('body1',str(tmp_path/'body1.c'),source)],
        use_system_cpp=False,use_compile_cache=False)
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:False)
    output=tmp_path/'body1'
    with pytest.raises(LinkError,match=r"undefined Mach-O symbol.*'_f'"):
        evaluator.emit_executable(units,str(output),optimize=False)
    assert not output.exists()
    assert not Path(str(output)+'.pcc-link.tmp').exists()


@pytest.mark.parametrize('representation',['native','raw','packed','decoded','source_view'])
def test_weak_reference_survives_final_link_and_codec(representation,monkeypatch):
    obj=_object('_optional',weak=True)
    if representation=='raw': obj=obj.to_macho()
    elif representation=='packed': obj=decode_packed_native_object(encode_native_object(obj))
    elif representation=='decoded': obj=decode_native_object(encode_native_object(obj))
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:pytest.fail('weak import needs no provider'))
    image=macho_exec.link_executable([obj],_consume_inputs=representation=='source_view',_direct_source_view=representation=='source_view')
    assert _imports(image)=={'_optional':(1,True)}
    symbols=spec.parse_object(image).symbols()
    assert next(s for s in symbols if s['name']=='_optional')['n_desc']&0x40


def test_relocatable_weak_roundtrip_and_strong_reference_dominance(monkeypatch):
    weak=_object('_optional',True)
    merged=link_relocatable([weak])
    assert next(s for s in spec.parse_object(merged).symbols() if s['name']=='_optional')['n_desc']&0x40
    sections,undefined=assemble_file('.section __TEXT,__text,regular,pure_instructions\n.globl _other\n_other:\n bl _optional\n ret\n')
    strong=NativeObject.from_sections(sections,undefined=undefined)
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:False)
    with pytest.raises(LinkError,match="'_optional'"):
        macho_exec.link_executable([weak,strong])


def test_weak_reference_does_not_extract_archive_member(monkeypatch):
    weak=_object('_optional',True)
    sections,undefined=assemble_file('.section __TEXT,__text,regular,pure_instructions\n.globl _optional\n_optional:\n bl _archive_missing\n ret\n')
    provider=NativeObject.from_sections(sections,undefined=undefined).to_macho()
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:False)
    image=macho_exec.link_executable([weak],archives=[write_archive([('optional.o',provider)])])
    assert _imports(image)=={'_optional':(1,True)}


def test_dynamic_import_requires_its_selected_library_provider(monkeypatch):
    looked_up=[]
    def resolve(name):
        looked_up.append(name)
        return name=='_getpid'
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',resolve)
    image=macho_exec.link_executable([_object('_getpid')])
    assert looked_up==['_getpid']
    assert _imports(image)=={'_getpid':(1,False)}
    with pytest.raises(LinkError,match="'_missing'"):
        macho_exec.link_executable([_object('_missing')])


# Darwin arm64 libSystem and Mach-O execution required
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != 'darwin' or os.uname().machine != 'arm64'))
def test_real_libsystem_import_executes(tmp_path):
    image=macho_exec.link_executable([_object('_getpid')])
    executable=tmp_path/'getpid';executable.write_bytes(image);executable.chmod(0o755)
    process=subprocess.Popen([str(executable)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    _,stderr=process.communicate(timeout=10)
    assert process.returncode==process.pid%256,stderr


# non-Darwin boundary
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform=='darwin'))
def test_cross_host_cannot_guess_darwin_exports():
    with pytest.raises(LinkError,match='target libSystem is unavailable'):
        macho_exec.link_executable([_object('_f')])


def test_failed_link_keeps_existing_output(tmp_path,monkeypatch):
    evaluator=CEvaluator(target_triple='arm64-apple-darwin',backend='self')
    units=evaluator.compile_translation_units(
        [TranslationUnit('body1','body1.c','int f(void); int main(void) { return f(); }')],
        use_system_cpp=False,use_compile_cache=False)
    output=tmp_path/'program';output.write_bytes(b'previous successful executable')
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:False)
    with pytest.raises(LinkError,match="'_f'"):
        evaluator.emit_executable(units,str(output),optimize=False)
    assert output.read_bytes()==b'previous successful executable'
    assert not Path(str(output)+'.pcc-link.tmp').exists()


def test_weak_state_changes_incremental_native_and_packed_identity():
    import hashlib
    from pcc.backend.macho_incremental import _hash_native_shape, _hash_packed_native_shape
    strong=_object('_optional');weak=_object('_optional',True)
    digests=[]
    for obj in [strong,weak]:
        native_digest=hashlib.sha256();_hash_native_shape(native_digest,obj)
        packed_digest=hashlib.sha256();_hash_packed_native_shape(packed_digest,decode_packed_native_object(encode_native_object(obj)))
        assert native_digest.digest()==packed_digest.digest()
        digests.append(native_digest.digest())
    assert digests[0]!=digests[1]


# Linux x86-64 emitted execution required
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform!='linux' or os.uname().machine!='x86_64'))
def test_owned_elf_c_missing_definition_resolved_definition_and_weak_zero_execute(tmp_path):
    from pcc.backend.x86_64_asm_driver import assemble_file as assemble_elf
    from pcc.backend.elf_x86_64 import ElfError,link_static_executable,STB_WEAK
    evaluator=CEvaluator(target_triple='x86_64-unknown-linux-gnu',backend='self')
    def compile_c(name,source):
        units=evaluator.compile_translation_units([TranslationUnit(name,name+'.c',source)],use_system_cpp=False,use_compile_cache=False)
        return assemble_elf(evaluator._self_backend_asm_text(units))
    start=assemble_elf('.intel_syntax noprefix\n.text\n.globl _start\n_start:\n call main\n mov edi, eax\n mov eax, 60\n syscall\n')
    body=compile_c('body1','int f(void); int main(void) { return f(); }')
    with pytest.raises(ElfError,match='undefined static ELF symbols: f'):
        link_static_executable([start,body])
    provider=compile_c('provider','int f(void) { return 42; }')
    def run(name,objects,expected):
        output=tmp_path/name;output.write_bytes(link_static_executable(objects));output.chmod(0o755)
        process=subprocess.run([str(output)],capture_output=True,timeout=10,env=dict(os.environ,PATH='/nonexistent'))
        assert process.returncode==expected,process.stderr
    run('resolved',[start,body,provider],42)
    weak=assemble_elf('.intel_syntax noprefix\n.text\n.globl _start\n_start:\n mov rax, qword ptr optional_pointer[rip]\n mov edi, eax\n mov eax, 60\n syscall\n.data\noptional_pointer:\n .quad optional\n')
    weak=replace(weak,symbols=tuple(replace(s,binding=STB_WEAK) if s.name=='optional' else s for s in weak.symbols))
    run('weak-zero',[weak],0)


def test_cached_image_revalidates_actual_dynamic_provider(tmp_path,monkeypatch):
    from pcc.backend.macho_incremental import IncrementalMachOLinker
    linker=IncrementalMachOLinker(tmp_path/'cache','test-source-identity')
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:True)
    obj=_object('_getpid')
    assert _imports(linker.link([obj]))=={'_getpid':(1,False)}
    monkeypatch.setattr(macho_exec,'_libsystem_exports_symbol',lambda name:False)
    with pytest.raises(LinkError,match="'_getpid'"):
        linker.link([obj])
    assert linker.stats.image_hits==0


# Darwin arm64 weak-bind execution required
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != 'darwin' or os.uname().machine != 'arm64'))
def test_missing_weak_function_pointer_executes_as_null(tmp_path):
    sections,undefined=assemble_file(
        '.section __TEXT,__text,regular,pure_instructions\n.globl _main\n_main:\n'
        ' adrp x0, _pcc_test_optional_undefined@GOTPAGE\n'
        ' ldr x0, [x0, _pcc_test_optional_undefined@GOTPAGEOFF]\n ret\n')
    obj=NativeObject.from_sections(sections,undefined=undefined,weak_undefined=('_pcc_test_optional_undefined',))
    path=tmp_path/'weak-null';path.write_bytes(macho_exec.link_executable([obj]));path.chmod(0o755)
    assert subprocess.run([str(path)],capture_output=True,timeout=10).returncode==0
