"""``test.support.script_helper`` for the conformance harness.

Running another interpreter is not part of a conformance run: every helper
that would spawn one skips. Writing script files works as in CPython.
"""

import os
import os.path
import unittest


def _no_interpreter():
    raise unittest.SkipTest("spawning an interpreter is not part of a conformance run")


def interpreter_requires_environment():
    return False


def run_python_until_end(*args, **env_vars):
    _no_interpreter()


def assert_python_ok(*args, **env_vars):
    _no_interpreter()


def assert_python_failure(*args, **env_vars):
    _no_interpreter()


def spawn_python(*args, **kw):
    _no_interpreter()


def kill_python(p):
    _no_interpreter()


def run_test_script(script):
    _no_interpreter()


def make_script(script_dir, script_basename, source, omit_suffix=False):
    script_filename = script_basename
    if not omit_suffix:
        script_filename += os.extsep + "py"
    script_name = os.path.join(script_dir, script_filename)
    if isinstance(source, str):
        with open(script_name, "w", encoding="utf-8") as script_file:
            script_file.write(source)
    else:
        with open(script_name, "wb") as script_file:
            script_file.write(source)
    return script_name


def make_zip_script(zip_dir, zip_basename, script_name, name_in_zip=None):
    _no_interpreter()


def make_pkg(pkg_dir, init_source=""):
    os.mkdir(pkg_dir)
    make_script(pkg_dir, "__init__", init_source)


def make_zip_pkg(*args, **kwargs):
    _no_interpreter()
