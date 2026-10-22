"""Industrial-grade cross-module ABI stress tests."""
import subprocess
import textwrap
import pytest

def _run_multi(tmp_path, modules):
    from pcc.frontends.python.pipeline import compile_python_multi
    srcs = []; mods = []
    for name, content in modules.items():
        p = tmp_path / f"{name}.py"; p.write_text(textwrap.dedent(content).strip(), encoding="utf-8")
        srcs.append(str(p)); mods.append(name)
    exe = tmp_path / "app.out"
    compile_python_multi(srcs, str(exe), module_names=mods, entry_module="main", ir_scaffold_mode="on", libpython_mode="off")
    return subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip()

def test_shadowing_repro(tmp_path):
    mods = {
        "main": (
            "import mod_x\n"
            "import mod_y\n"
            "def main():\n"
            "    print(mod_x.W(1).work(), mod_y.W(2).work())\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        ),
        "mod_x": (
            "class W:\n"
            "    def __init__(self, v):\n"
            "        self.v = v\n"
            "    def work(self):\n"
            "        return self.v + 10\n"
        ),
        "mod_y": (
            "class W:\n"
            "    def __init__(self, v):\n"
            "        self.v = v\n"
            "    def work(self):\n"
            "        return self.v * 100\n"
        ),
    }
    assert _run_multi(tmp_path, mods) == "11 200"

def test_massive_layout_repro(tmp_path):
    fields = "\n        ".join([f"self.f{i} = {i}" for i in range(128)])
    mods = {
        "main": (
            "from lib import H\n"
            "def main():\n"
            "    h = H()\n"
            "    print(h.f0, h.f63, h.f127)\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        ),
        "lib": f"class H:\n    def __init__(self):\n        {fields}"
    }
    assert _run_multi(tmp_path, mods) == "0 63 127"
