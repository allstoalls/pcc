"""Annotated pass lists must invoke the runtime subclass's run method."""
import os
import subprocess
import pytest
from pcc.py_frontend.pipeline import compile_python_multi


@pytest.mark.parametrize("alias_and_transitive", [False, True])
def test_cross_module_annotated_pass_list_keeps_overrides(tmp_path, pcc_py_runtime_archive,
                                                       alias_and_transitive):
    base = tmp_path / 'pass_base.py'
    base.write_text('''from abc import ABC, abstractmethod
class Base(ABC):
    @abstractmethod
    def run(self, value: str) -> str:
        ...
class Pipeline:
    def __init__(self):
        self.passes: list[Base] = []
    def apply(self, value: str) -> str:
        for item in self.passes:
            value = item.run(value)
        return value
''', encoding='utf-8')
    derived = tmp_path / 'pass_impl.py'
    implementation = '''from pass_base import Base
class First(Base):
    def run(self, value: str) -> str:
        return value + "-first"
class Second(Base):
    def run(self, value: str) -> str:
        return value + "-second"
'''
    if alias_and_transitive:
        implementation = implementation.replace(
            'from pass_base import Base\n',
            'from pass_base import Base as ImportedBase\nclass Middle(ImportedBase):\n    pass\n',
        ).replace('First(Base)', 'First(Middle)').replace('Second(Base)', 'Second(ImportedBase)')
    derived.write_text(implementation, encoding='utf-8')
    entry = tmp_path / 'pass_entry.py'
    entry.write_text('''from pass_base import Pipeline
from pass_impl import First, Second
def main():
    pipeline = Pipeline()
    pipeline.passes.append(First())
    pipeline.passes.append(Second())
    print(pipeline.apply("value"))
main()
''', encoding='utf-8')
    binary = tmp_path / 'pass_dispatch'
    compile_python_multi([str(base), str(derived), str(entry)], str(binary),
                         module_names=['pass_base', 'pass_impl', 'pass_entry'],
                         entry_module='pass_entry', backend='self', libpython_mode='off',
                         runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == 'value-first-second\n', f'GC{gc}: {result.stdout}'


def test_mixin_calls_helper_supplied_by_runtime_subclass(tmp_path, pcc_py_runtime_archive):
    mixin = tmp_path / 'helper_mixin.py'
    mixin.write_text('''events = []
def argument(value):
    events.append(value)
    return value
class Mixin:
    def apply(self, value):
        return self.helper(argument(value))
''', encoding='utf-8')
    entry = tmp_path / 'helper_entry.py'
    entry.write_text('''from helper_mixin import Mixin, events
class Plus(Mixin):
    def helper(self, value):
        return value + 1
class Double(Mixin):
    def helper(self, value):
        return value * 2
def main():
    print(Plus().apply(4), Double().apply(4))
    try:
        Mixin().apply(4)
    except AttributeError:
        print("missing")
    print(events)
main()
''', encoding='utf-8')
    binary = tmp_path / 'mixin_helpers'
    compile_python_multi([str(mixin), str(entry)], str(binary),
                         module_names=['helper_mixin', 'helper_entry'], entry_module='helper_entry',
                         backend='self', libpython_mode='off', runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == '5 8\nmissing\n[4, 4]\n', f'GC{gc}: {result.stdout}'
