"""Discovery parity before exports, preserving normal provider admission."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import probe_stage1_closure as tool


class DiscoveryFinished(Exception):
    pass


def _production_nodes(entry, monkeypatch, **options):
    """Observe the production boundary after its final discovery pass."""
    from pcc.frontends.python import pipeline

    captured = []
    prepare = pipeline._prepare_multi_source_compile_closure

    def observe(*args, **kwargs):
        sources, modules = prepare(*args, **kwargs)
        captured.extend(zip(modules, sources))
        raise DiscoveryFinished

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "_prepare_multi_source_compile_closure", observe)
        with pytest.raises(DiscoveryFinished):
            effective = {
                "backend": "self", "libpython_mode": "off", "ir_scaffold_mode": "on",
                **options,
            }
            pipeline.compile_python(
                str(entry), str(entry.parent / "never-produced"), **effective,
            )
    return set(captured)


@pytest.fixture
def package_entry(tmp_path):
    package = tmp_path / "discovery_package"
    package.mkdir()
    sources = {
        "__init__": "",
        "__main__": "from discovery_package.bootstrap import run\nrun()\n",
        "bootstrap": (
            "def run():\n"
            "    from discovery_package.worker import answer\n"
            "    return answer()\n"
        ),
        "worker": "from .detail import VALUE\ndef answer(): return VALUE\n",
        "detail": (
            "import sys\n"
            "if sys.implementation.name == 'cpython':\n"
            "    from discovery_package.host_only import VALUE\n"
            "VALUE = 42\n"
        ),
        "host_only": "VALUE = 0\n",
    }
    for name, source in sources.items():
        (package / (name + ".py")).write_text(source, encoding="utf-8")
    return package / "__main__.py"


def test_probe_matches_production_discovery_node_set(package_entry, monkeypatch):
    production = _production_nodes(package_entry, monkeypatch)
    sources, modules = tool._tightened_closure(str(package_entry))
    assert set(zip(modules, sources)) == production
    assert "discovery_package.worker" in modules
    assert "discovery_package.detail" in modules
    assert "discovery_package.host_only" not in modules
    assert not (package_entry.parent / "never-produced").exists()


@pytest.mark.parametrize("libpython_mode", ("off", "auto", "on"))
@pytest.mark.parametrize("ir_scaffold_mode", ("on", "off"))
def test_probe_uses_the_selected_production_modes(
    package_entry, monkeypatch, libpython_mode, ir_scaffold_mode,
):
    options = {"libpython_mode": libpython_mode, "ir_scaffold_mode": ir_scaffold_mode}
    production = _production_nodes(package_entry, monkeypatch, **options)
    sources, modules, effective, _admissions = tool._discover_stage1_closure(str(package_entry), **options)
    assert set(zip(modules, sources)) == production
    assert effective["libpython_mode"] == libpython_mode
    assert effective["ir_scaffold_mode"] == ir_scaffold_mode
    assert ("discovery_package.worker" in modules) is (libpython_mode == "off")


def test_owned_provider_roots_and_recursive_admission_match_production(
    package_entry, monkeypatch, tmp_path,
):
    from pcc.frontends.python import pipeline_dependency_closure as closure

    provider_root = tmp_path / "provider_pcc"
    stdlib = provider_root / "stdlib"
    stdlib.mkdir(parents=True)
    (stdlib / "__init__.py").write_text("", encoding="utf-8")
    (stdlib / "subprocess.py").write_text(
        "from provider_tail import VALUE\n", encoding="utf-8",
    )
    (stdlib / "provider_tail.py").write_text("VALUE = 42\n", encoding="utf-8")
    with package_entry.open("a", encoding="utf-8") as stream:
        stream.write("import subprocess\n")
    monkeypatch.setattr(closure, "_pcc_package_dir_candidates", lambda: [str(provider_root)])
    production = _production_nodes(package_entry, monkeypatch)
    sources, modules, options, _admissions = tool._discover_stage1_closure(str(package_entry))
    assert set(zip(modules, sources)) == production
    assert {"subprocess", "provider_tail"} <= set(modules)
    assert options["recursive_stdlib_requested"] is False
    assert options["recursive_stdlib_effective"] is True


@pytest.mark.parametrize("scaffold,provider", (("on", "pcc.ir.ir"), ("off", "pcc.ir.compat")))
def test_scaffold_provider_selection_matches_production(monkeypatch, tmp_path, scaffold, provider):
    package = tmp_path / "pcc"
    (package / "ir").mkdir(parents=True)
    for path in (package / "__init__.py", package / "ir" / "__init__.py"):
        path.write_text("", encoding="utf-8")
    entry = package / "__main__.py"
    entry.write_text("from pcc.ir.compat import VALUE\n", encoding="utf-8")
    (package / "ir" / "compat.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "ir" / "ir.py").write_text("VALUE = 2\n", encoding="utf-8")
    production = _production_nodes(entry, monkeypatch, ir_scaffold_mode=scaffold)
    sources, modules, _options, _admissions = tool._discover_stage1_closure(
        str(entry), ir_scaffold_mode=scaffold,
    )
    assert set(zip(modules, sources)) == production
    assert set(modules) & {"pcc.ir.ir", "pcc.ir.compat"} == {provider}


def test_direct_indexed_single_entry_uses_multi_admission(monkeypatch, tmp_path):
    entry = tmp_path / "single_entry.py"
    entry.write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    production = _production_nodes(entry, monkeypatch)
    sources, modules, options, _admissions = tool._discover_stage1_closure(str(entry))
    assert set(zip(modules, sources)) == production
    assert options["direct_indexed_assembly"] is True


def test_host_provider_trial_codegen_fails_closed_and_restores_admission(
    package_entry, monkeypatch, tmp_path,
):
    from pcc.frontends.python import pipeline_dependency_closure as closure

    host = tmp_path / "host_provider.py"
    host.write_text("VALUE = 42\n", encoding="utf-8")
    with package_entry.open("a", encoding="utf-8") as stream:
        stream.write("import host_provider\n")
    monkeypatch.setattr(
        closure, "_locate_stdlib_module_source",
        lambda module: str(host) if module == "host_provider" else None,
    )

    def no_codegen(*args, **kwargs):
        pytest.fail("discovery invoked trial codegen")

    monkeypatch.setattr(closure, "_stdlib_module_compiles", no_codegen)
    with pytest.raises(tool.DiscoveryRequiresCodegen, match="host_provider"):
        tool._discover_stage1_closure(
            str(package_entry), recursive_stdlib=True, lexical_only=True,
        )
    assert closure._stdlib_module_compiles is no_codegen
    error_receipt = tmp_path / "lexical-error.json"
    assert tool.main([
        "--entry", str(package_entry), "--recursive-stdlib", "--lexical-only",
        "--json", str(error_receipt),
    ]) == 1
    blocked = json.loads(error_receipt.read_text())
    assert blocked["status"] == "ERROR"
    assert blocked["provider_admission_mode"] == "lexical_only"
    assert str(host) in blocked["error"]
    assert "nodes" not in blocked and "module_count" not in blocked
    assert closure._stdlib_module_compiles is no_codegen

    attempts = []

    def reject_provider(source, module):
        attempts.append((source, module))
        return False

    monkeypatch.setattr(closure, "_stdlib_module_compiles", reject_provider)
    _sources, modules, options, admissions = tool._discover_stage1_closure(
        str(package_entry), recursive_stdlib=True,
    )
    assert attempts == [(str(host), "host_provider")]
    assert "host_provider" not in modules
    assert options["provider_admission_mode"] == "production"
    assert admissions == [{
        "module": "host_provider", **tool._source_identity(host),
        "trial_codegen": True, "status": "REJECTED",
    }]
    assert closure._stdlib_module_compiles is reject_provider


def test_main_only_discovers_and_hashes_the_production_nodes(
    package_entry, monkeypatch, tmp_path, capsys,
):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    production = _production_nodes(package_entry, monkeypatch)

    def no_codegen(*args, **kwargs):
        pytest.fail("discovery CLI invoked codegen")

    monkeypatch.setattr(tool, "_try_per_module_lower", no_codegen)
    monkeypatch.setattr(tool, "_try_full_multi_compile", no_codegen)
    monkeypatch.setattr(L1CodeGen, "generate", no_codegen)
    receipt_path = tmp_path / "receipt.json"
    assert tool.main(["--entry", str(package_entry), "--json", str(receipt_path)]) == 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["scope"] == "source_discovery"
    assert receipt["stage1_qualified"] is False
    assert set((node["module"], node["path"]) for node in receipt["nodes"]) == production
    for node in receipt["nodes"]:
        with open(node["path"], "rb") as stream:
            assert node["sha256"] == hashlib.sha256(stream.read()).hexdigest()
    canonical = json.dumps(receipt["nodes"], sort_keys=True, separators=(",", ":"))
    assert receipt["ordered_nodes_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert "stage1_module_codegen" in receipt["not_run"]
    assert "Discovery only" in capsys.readouterr().out


def test_package_abi_failure_has_an_error_receipt(package_entry, monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline

    def reject(*args, **kwargs):
        raise pipeline.PyPipelineError("PCC-PACKAGE-CPYTHON-ABI: fixture dependency")

    monkeypatch.setattr(pipeline, "_validate_package_site_no_libpython_abi", reject)
    receipt_path = tmp_path / "failure.json"
    assert tool.main(["--entry", str(package_entry), "--json", str(receipt_path)]) == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "ERROR"
    assert "PCC-PACKAGE-CPYTHON-ABI" in receipt["error"]
    assert receipt["stage1_qualified"] is False
    assert "nodes" not in receipt


def test_obsolete_walker_modes_are_rejected_before_discovery(monkeypatch):
    monkeypatch.setenv("PCC_PROBE_CLOSURE", "max")
    monkeypatch.setattr(tool, "_discover_stage1_closure", lambda *a, **kw: pytest.fail("discovered"))
    with pytest.raises(SystemExit) as error:
        tool.main([])
    assert error.value.code == 2


def test_repository_entry_matches_production_discovery_nodes(monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline

    root = Path(__file__).resolve().parents[2]
    entry = root / "pcc" / "__main__.py"
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(root))
    monkeypatch.setenv("PCC_REPO_ROOT", str(root))
    monkeypatch.delenv("PCC_PY_STDLIB_ROOT", raising=False)

    def no_stage1_codegen(*args, **kwargs):
        pytest.fail("repository discovery invoked Stage1 module codegen")

    def inventory():
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "pcc").rglob("*.py"))
        }

    monkeypatch.setattr(pipeline, "_compile_python_multi_codegen_parallel", no_stage1_codegen)
    before = inventory()
    production = _production_nodes(entry, monkeypatch)
    sources, modules, options, admissions = tool._discover_stage1_closure(
        str(entry), libpython_mode="off", ir_scaffold_mode="on",
    )
    assert set(zip(modules, sources)) == production
    assert inventory() == before, "source changed during discovery"
    receipt = tool._closure_receipt(sources, modules, options, admissions)
    (tmp_path / "probe-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    production_nodes = [
        {"module": module, **tool._source_identity(source)}
        for module, source in sorted(production)
    ]
    (tmp_path / "production-nodes.json").write_text(
        json.dumps(production_nodes, indent=2) + "\n",
    )
