import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_no_init_class_call_accepts_keyword_attrs_without_libpython(tmp_path):
    src = tmp_path / "class_kwargs_no_init.py"
    src.write_text(
        textwrap.dedent(
            """
            class Box:
                pass

            b = Box(label="x", count=3)
            print(b.label)
            print(b.count)
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "class_kwargs_no_init.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["x", "3"]


def test_no_init_field_class_accepts_extra_keyword_attrs_without_libpython(tmp_path):
    src = tmp_path / "field_class_extra_kwargs_no_init.py"
    src.write_text(
        textwrap.dedent(
            """
            class Box:
                x = 0

            b = Box(4, label="x")
            print(b.label)
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "field_class_extra_kwargs_no_init.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["x"]


def test_self_dunder_class_constructor_starstar_kwargs_without_libpython(tmp_path):
    src = tmp_path / "self_dunder_class_ctor_starstar.py"
    src.write_text(
        textwrap.dedent(
            """
            class Config:
                def __init__(self, distutils_section=None, noopt=None, noarch=None):
                    self.distutils_section = distutils_section
                    self.noopt = noopt
                    self.noarch = noarch
                    self._conf_keys = {"noopt": noopt, "noarch": noarch}

                def clone(self):
                    return self.__class__(
                        distutils_section=self.distutils_section,
                        **self._conf_keys,
                    )

            c = Config("build", "0", "1")
            d = c.clone()
            print(d.distutils_section)
            print(d.noopt)
            print(d.noarch)
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "self_dunder_class_ctor_starstar.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    out = subprocess.check_output([str(exe)], text=True).splitlines()
    assert out == ["build", "0", "1"]
