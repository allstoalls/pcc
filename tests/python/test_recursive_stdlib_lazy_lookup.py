"""Lazy provider discovery must not import unrelated host packages."""

import sys


def test_lazy_lookup_does_not_execute_host_package(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline_dependency_closure as closure

    package = tmp_path / "unrelated_lazy_dependency"
    package.mkdir()
    marker = tmp_path / "imported"
    (package / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n", encoding="utf-8"
    )
    (package / "child.py").write_text("VALUE = 42\n", encoding="utf-8")
    source = tmp_path / "application.py"
    source.write_text(
        "def optional_feature():\n"
        "    import unrelated_lazy_dependency.child\n"
        "    return unrelated_lazy_dependency.child.VALUE\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PCC_HOST_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    sources = [str(source)]
    modules = ["application"]
    closure._expand_recursive_stdlib(sources, modules, {"application": str(source)})

    assert not marker.exists(), "native-only discovery executed a host package"
    assert sources == [str(source)]
    assert modules == ["application"]
