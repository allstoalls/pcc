"""Inherited ordinary dataclass defaults preserve identity and evaluation count.

Native cases use the owned self backend without libpython, with separate
pcc0 and pcc1 fixture parameters. Every emitted binary executes under GC0-4.
PCC_RUNTIME_ARCHIVE must explicitly select the matching immutable archive;
PCC_NO_AUTO_PCC1=1 and PCC_TEST_NO_NATIVE_PROVISIONING=1 are the suite gates.
"""

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


TUPLE = r"""from dataclasses import dataclass
@dataclass
class Base:
    fields: tuple = ()
@dataclass
class Child(Base):
    pass
def main():
    assert Child().fields == ()
    print('INHERITED_TUPLE_DEFAULT_OK')
main()
"""


IDENTITY = r"""from dataclasses import dataclass, field
events = []
factory_events = []
class Token:
    pass
def make(label):
    events.append(label)
    return Token()
def make_rows():
    factory_events.append('rows')
    return []
@dataclass
class Base:
    required: object
    fields: tuple = ()
    token: object = make('token')
    kind: object = Token
    optional: object = None
    number: int = 31
    wrapped: object = field(default=make('wrapped'))
    rows: object = field(default_factory=make_rows)
original_token = Base.token
original_wrapped = Base.wrapped
fields = ('shadow',)
token = 'shadow'
kind = 'shadow'
Base.token = Token()
@dataclass
class Child(Base):
    child: bool = True
def replacement_init(self):
    raise AssertionError('inherited defaults used rebound __init__')
Child.__init__ = replacement_init
@dataclass
class Grandchild(Child):
    grandchild: bool = True
def main():
    assert events == ['token', 'wrapped']
    assert factory_events == []
    first = Grandchild('first')
    constructor = Grandchild
    second = constructor('second')
    assert first.fields == ()
    assert first.fields is second.fields
    assert first.token is original_token
    assert second.token is original_token
    assert first.wrapped is original_wrapped
    assert second.wrapped is original_wrapped
    assert first.kind is Token
    assert first.optional is None
    assert first.number == 31
    assert first.child and first.grandchild
    assert first.rows is not second.rows
    assert events == ['token', 'wrapped']
    assert factory_events == ['rows', 'rows']
    supplied = Token()
    explicit = constructor('explicit', token=supplied, optional=supplied, rows=None)
    assert explicit.token is supplied
    assert explicit.optional is supplied
    assert explicit.rows is None
    assert factory_events == ['rows', 'rows']
    try:
        constructor()
        raise AssertionError('inherited required field became optional')
    except TypeError:
        pass
    print('INHERITED_DEFAULT_IDENTITY_OK')
main()
"""


FACTORY_ERROR = r"""from dataclasses import dataclass, field
events = []
def make():
    events.append('factory')
    if len(events) == 1:
        raise ValueError('factory')
    return []
@dataclass
class Base:
    fields: tuple = ()
    rows: object = field(default_factory=make)
@dataclass
class Child(Base):
    pass
def main():
    assert events == []
    try:
        Child()
        raise AssertionError('factory error was lost')
    except ValueError:
        pass
    second = Child()
    assert second.fields == ()
    assert second.rows == []
    explicit = Child(rows=None)
    assert explicit.rows is None
    assert events == ['factory', 'factory']
    print('INHERITED_FACTORY_ERROR_OK')
main()
"""


OVERRIDES = r"""from dataclasses import dataclass, field
events = []
class Token:
    pass
def make(label):
    events.append(label)
    return Token()
def base_rows():
    events.append('base_rows')
    return []
def child_rows():
    events.append('child_rows')
    return []
@dataclass
class Base:
    required: object
    token: object = make('base')
    rows: object = field(default_factory=base_rows)
original = Base.token
Base.__init__.__defaults__ = ()
def replacement(self):
    self.required = 'rebound'
Base.__init__ = replacement
Base.token = Token()
@dataclass
class Child(Base):
    token: object = make('child')
    rows: object = field(default_factory=child_rows)
child_default = Child.token
Child.__init__.__defaults__ = ()
@dataclass
class Grandchild(Child):
    enabled: bool = True
@dataclass
class Custom:
    required: object
    token: object = make('custom')
    rows: object = field(default_factory=base_rows)
    def __init__(self):
        self.required = 'custom'
custom_default = Custom.token
@dataclass
class InheritsCustom(Custom):
    pass
def main():
    assert events == ['base', 'child', 'custom']
    assert Base().required == 'rebound'
    assert Custom().required == 'custom'
    first = Grandchild('first')
    constructor = Grandchild
    second = constructor('second')
    assert first.required == 'first'
    assert first.token is child_default
    assert second.token is child_default
    assert first.token is not original
    assert first.rows is not second.rows
    assert first.enabled and second.enabled
    supplied = Token()
    explicit = constructor('explicit', supplied, None, False)
    assert explicit.token is supplied
    assert explicit.rows is None
    assert explicit.enabled is False
    inherited = InheritsCustom('inherited')
    assert inherited.required == 'inherited'
    assert inherited.token is custom_default
    assert inherited.rows == []
    assert events == ['base', 'child', 'custom', 'child_rows', 'child_rows', 'base_rows']
    try:
        constructor()
        raise AssertionError('required override field was lost')
    except TypeError:
        pass
    print('INHERITED_DECLARATION_OVERRIDES_OK')
main()
"""


PREPARED_OVERRIDES = OVERRIDES.replace('@dataclass\nclass Custom:', '''class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        assert '__pcc_dataclass_defaults__' not in namespace
        return type(name, bases, namespace)
@dataclass
class Custom(metaclass=Meta):''')


PROGRAMS = {
    'tuple': (
        TUPLE,
        'INHERITED_TUPLE_DEFAULT_OK\n'
    ),
    'identity': (
        IDENTITY,
        'INHERITED_DEFAULT_IDENTITY_OK\n'
    ),
    'factory_error': (
        FACTORY_ERROR,
        'INHERITED_FACTORY_ERROR_OK\n'
    ),
    'overrides': (OVERRIDES, 'INHERITED_DECLARATION_OVERRIDES_OK\n'),
    'prepared_overrides': (PREPARED_OVERRIDES, 'INHERITED_DECLARATION_OVERRIDES_OK\n'),
}


@pytest.mark.parametrize("case", tuple(PROGRAMS))
def test_inherited_default_reference(case, tmp_path):
    program, expected = PROGRAMS[case]
    assert_reference_program(program, expected, tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("case", tuple(PROGRAMS))
def test_inherited_default_native_five_gc(
    case,
    tmp_path,
    explicit_owned_runtime,
    python_program_compiler,
    request,
    capfd,
):
    program, expected = PROGRAMS[case]
    assert_owned_program(
        program,
        expected,
        tmp_path,
        python_program_compiler,
        request.node.callspec.params["python_program_compiler"],
        explicit_owned_runtime,
        capfd,
        provenance_probe='2',
    )


@pytest.mark.parametrize('case', tuple(PROGRAMS))
def test_inherited_default_reads_class_declaration_ir(case, tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    source = tmp_path / 'declaration.py'
    output = tmp_path / 'declaration.ll'
    source.write_text(PROGRAMS[case][0])
    compile_python(str(source), str(output), emit_llvm_only=True, backend='self',
                   libpython_mode='off', ir_scaffold_mode='on')
    text = output.read_text()
    assert '__pcc_dataclass_defaults__' in text
    assert 'dataclass.base.declaration' in text
    assert 'dataclass.base.init' not in text
    assert 'dataclass.base.defaults' not in text
    assert 'strict.nolib.stub' not in text


@pytest.mark.parametrize('case', ('inherited_capture', 'prepared_order'))
def test_factory_declaration_capture_ir(case, tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    from tests.python.test_dataclass_factory_binding import PROGRAMS as factories
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    source = tmp_path / 'factory_declaration.py'
    output = tmp_path / 'factory_declaration.ll'
    source.write_text(factories[case][0])
    compile_python(str(source), str(output), emit_llvm_only=True, backend='self',
                   libpython_mode='off', ir_scaffold_mode='on')
    text = output.read_text()
    assert '__pcc_dataclass_defaults__' in text
    assert 'dataclass.declaration.defaults' in text
    assert 'dataclass.base.init' not in text
    assert 'strict.nolib.stub' not in text
