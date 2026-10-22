"""Runtime process statuses cross the declared Python constructor ABI once."""
import os
from pathlib import Path
import re

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.type_infer import infer_module
from pcc.frontends.python.pipeline_context import build_closed_world_context


PROVIDER = '''class CalledProcessError(Exception):
    def __init__(self, returncode: int, cmd, output=None, stderr=None) -> None:
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
        self.stderr = stderr
'''
CALLS = {
    "run": "subprocess.run(argv, check=True)",
    "timeout": "subprocess.run(argv, check=True, timeout=1)",
    "check_call": "return subprocess.check_call(argv)",
}


def _generate(module, exports):
    module = infer_module(module, external_exports={key: value for key, value in exports.items() if key != module.name})
    codegen = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    text = str(codegen.generate(module))
    assert emit_owned_object(text, "x86_64-unknown-linux-gnu")[:4] == b"\x7fELF"
    return text


@pytest.mark.parametrize("shape", CALLS)
@pytest.mark.parametrize("machine_provider", [False, True])
def test_process_status_matches_actual_constructor_operand_abi(tmp_path, monkeypatch, shape, machine_provider):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    # An explicitly marked runtime provider is a real machine-ABI control;
    # ordinary Python providers must keep the boxed arbitrary-precision ABI.
    provider_name = "pcc.runtime.py.status_provider" if machine_provider else "subprocess"
    provider = tmp_path / "provider.py"
    provider.write_text(("__pcc_runtime_port__ = True\n" if machine_provider else "") + PROVIDER)
    entry = tmp_path / "entry.py"
    entry.write_text("import subprocess\ndef main(argv: list[str]):\n    " + CALLS[shape] + "\n")
    modules, exports, _derived = build_closed_world_context(
        [str(provider), str(entry)], [provider_name, "entry"]
    )
    provider_export = exports[provider_name]["CalledProcessError"]
    init_export = next(method for method in provider_export["methods"] if method["name"] == "__init__")
    assert init_export["box_int_abi"] is (not machine_provider)
    provider_ir = _generate(modules[0], exports)
    caller_exports = dict(exports)
    caller_exports["subprocess"] = exports[provider_name]
    caller_ir = _generate(modules[1], caller_exports)
    symbol = init_export["symbol"]
    definition = re.search(r"^define [^\n]*@" + symbol + r"\(([^\n]*)\)", provider_ir, re.M)
    declaration = re.search(r"^declare [^\n]*@" + symbol + r"\(([^\n]*)\)", caller_ir, re.M)
    call = re.search(r"\bcall [^\n]*@" + symbol + r"\(([^\n]*)\)", caller_ir)
    assert definition and declaration and call, "\n".join(line for line in (provider_ir + caller_ir).splitlines() if "CalledProcessError" in line)
    status_lane = "i64" if machine_provider else "ptr"
    for signature in (definition, declaration, call):
        assert signature.group(1).split(",")[1].strip().startswith(status_lane), signature.group(0)
    (tmp_path / "provider.ll").write_text(provider_ir)
    (tmp_path / "caller.ll").write_text(caller_ir)
    definitions = dict(re.findall(r"(?m)^\s*(%[^\s=]+) = ([^\n]+)", caller_ir))
    raw_statuses = [name for name, value in definitions.items()
                    if re.search(r"@py_subprocess_run(?:_timeout)?\(", value)]
    assert len(raw_statuses) == 1
    raw_status = raw_statuses[0]
    boxes = [name for name, value in definitions.items()
             if "@py_int_from_i64(i64 " + raw_status + ")" in value]
    status_operand = call.group(1).split(",")[1].strip().split()[-1]
    assert len(boxes) == (0 if machine_provider else 1) + (shape == "check_call")
    if machine_provider:
        assert status_operand == raw_status
    else:
        # The initializer receives a reload of the owned boxed-status root.
        # Trace its exact slot and stored value, not a guessed SSA name.
        load = definitions[status_operand]
        assert "@pcc_gc_load_ptr(" in load
        root_pointer = load.split("@pcc_gc_load_ptr(", 1)[1].rstrip(")").split(",")[-1].strip().split()[-1]
        def underlying(value):
            expression = definitions.get(value, "")
            return underlying(expression.split()[2]) if expression.startswith("bitcast ") else value
        rooted_boxes = []
        for line in caller_ir.splitlines():
            if "@pcc_gc_store_root(" not in line or "call" not in line:
                continue
            operands = line.split("@pcc_gc_store_root(", 1)[1].rstrip(")").split(",")
            pointer, value = [operand.strip().split()[-1] for operand in operands]
            if underlying(pointer) == underlying(root_pointer) and value in boxes:
                rooted_boxes.append(value)
        assert len(rooted_boxes) == 1
    assert not re.search(r"\bcall [^\n]*@py_cpy_", caller_ir)



NATIVE_SOURCE = '''import subprocess
def main():
    ok = ['/bin/true']
    bad = ['/bin/false']
    subprocess.run(ok, check=True)
    assert subprocess.check_call(ok) == 0
    caught = 0
    try:
        subprocess.run(bad, check=True)
    except subprocess.CalledProcessError as error:
        assert error.returncode == 1
        assert error.cmd is bad
        assert error.output is None and error.stderr is None
        caught += 1
    try:
        subprocess.check_call(bad)
    except subprocess.CalledProcessError as error:
        assert error.returncode == 1
        assert error.cmd is bad
        assert error.output is None and error.stderr is None
        caught += 1
    assert caught == 2
    assert subprocess.check_call(ok) == 0
    print('SUBPROCESS_STATUS_ABI_OK')
main()
'''


@pytest.mark.integration
# The application program runs /bin/true and /bin/false.
@pytest.mark.pcc_gate(probe=lambda: all(
    os.path.isfile(command) and os.access(command, os.X_OK) for command in ('/bin/true', '/bin/false')))
def test_subprocess_status_native_five_gc(tmp_path, monkeypatch, pcc_runtime_archive,
                                         python_program_compiler):
    import os
    import subprocess

    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    source = tmp_path / 'subprocess_status.py'
    binary = source.with_suffix('')
    source.write_text(NATIVE_SOURCE)
    python_program_compiler(str(source), str(binary), backend='self', libpython_mode='off',
                            ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PATH='/nonexistent', PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'SUBPROCESS_STATUS_ABI_OK\n'
        assert result.stderr == ''
