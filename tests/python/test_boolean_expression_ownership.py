"""Boolean operands transfer one owner through selected branches and cleanup.

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


LOCAL_OR = r"""def selected_local() -> int:
    value = 4611686018427387911
    return value or 1


def main() -> None:
    value = selected_local()
    print(value)
    print(value + 17)
    assert value == 4611686018427387911
    assert value + 17 == 4611686018427387928


main()
"""


INTEGER_BOOLEAN_JOINS = r"""import gc


def fresh(label: str, value: int) -> int:
    print(label)
    return value + 0


def borrowed_or_fresh(value: int) -> int:
    return value or fresh("or right", 1 << 80)


def fresh_or_borrowed(value: int) -> int:
    return fresh("or left", 1 << 80) or value


def borrowed_and_fresh(value: int) -> int:
    return value and fresh("and right", 1 << 80)


def fresh_and_borrowed(value: int) -> int:
    return fresh("and left", 1 << 80) and value


def nested(value: int) -> int:
    return (value or fresh("nested skipped", 1 << 80)) and value


def finally_or() -> int:
    value = 1 << 80
    try:
        return value or 1
    finally:
        value = 0
        gc.collect()


def main() -> None:
    value = 1 << 80
    assert borrowed_or_fresh(value) is value
    assert fresh_and_borrowed(value) is value
    assert nested(value) is value
    assert borrowed_or_fresh(0) == value
    assert fresh_or_borrowed(value) == value
    assert borrowed_and_fresh(value) == value
    assert borrowed_and_fresh(0) == 0
    assert fresh_and_borrowed(0) == 0
    assert finally_or() == value
    gc.collect()
    assert value == 1208925819614629174706176
    print("INTEGER_BOOLEAN_JOINS_OK")


main()
"""


OBJECT_ASSIGNMENT_JOINS = r"""import gc


class Tracked:
    def __init__(self, label: str, truth: bool) -> None:
        self.label = label
        self.truth = truth

    def __bool__(self) -> bool:
        print("truth " + self.label)
        return self.truth

    def __del__(self) -> None:
        print("finalize " + self.label)


def select_or(borrowed):
    result: object = Tracked("discarded or", False) or borrowed
    return result


def select_and(borrowed):
    result: object = Tracked("discarded and", True) and borrowed
    return result


def selected_new(borrowed):
    result: object = borrowed and Tracked("selected new", True)
    return result


def scope() -> None:
    value = Tracked("borrowed", True)
    assert select_or(value) is value
    assert select_and(value) is value
    result = selected_new(value)
    assert result.label == "selected new"
    del result
    gc.collect()


def main() -> None:
    scope()
    gc.collect()
    print("OBJECT_ASSIGNMENT_JOINS_OK")


main()
"""


GLOBAL_ASSIGNMENT_JOIN = r"""import gc


saved = None


class Tracked:
    def __init__(self, label: str) -> None:
        self.label = label

    def __del__(self) -> None:
        print("finalize " + self.label)


def capture(value) -> None:
    global saved
    saved = value or Tracked("fresh selected")


def main() -> None:
    global saved
    value = Tracked("selected")
    capture(value)
    assert saved is value
    saved = None
    del value
    gc.collect()
    capture(None)
    assert saved.label == "fresh selected"
    saved = None
    gc.collect()
    print("GLOBAL_ASSIGNMENT_JOIN_OK")


main()
"""


CONTAINER_ASSIGNMENT_JOIN = r"""import gc


class Tracked:
    def __init__(self, label: str, truth: bool) -> None:
        self.label = label
        self.truth = truth

    def __bool__(self) -> bool:
        return self.truth

    def __del__(self) -> None:
        print("finalize " + self.label)


def choose(value: Tracked) -> list:
    return [value or Tracked("fresh selected", True)]


def scope() -> None:
    borrowed = Tracked("borrowed selected", True)
    values = choose(borrowed)
    assert values[0] is borrowed
    del values
    del borrowed
    gc.collect()
    unused = Tracked("unselected", False)
    values = choose(unused)
    assert values[0].label == "fresh selected"
    del values
    del unused
    gc.collect()


def main() -> None:
    scope()
    gc.collect()
    print("CONTAINER_ASSIGNMENT_JOIN_OK")


main()
"""


FINALIZER_JOINS = r"""import gc


class Tracked:
    def __init__(self, label: str, truth: bool) -> None:
        self.label = label
        self.truth = truth

    def __bool__(self) -> bool:
        print("truth " + self.label)
        return self.truth

    def __del__(self) -> None:
        print("finalize " + self.label)


def fresh(label: str, truth: bool):
    print("make " + label)
    return Tracked(label, truth)


def selected_fresh_or(borrowed):
    return fresh("selected left", True) or borrowed


def discarded_fresh_or(borrowed):
    return fresh("discarded left", False) or borrowed


def selected_fresh_and(borrowed):
    return fresh("selected false", False) and borrowed


def discarded_fresh_and(borrowed):
    return fresh("discarded true", True) and borrowed


def borrowed_or_fresh(borrowed):
    return borrowed or fresh("selected right", True)


def borrowed_and_fresh(borrowed):
    return borrowed and fresh("selected and", True)


def conditional(borrowed, choose_borrowed: bool):
    return borrowed if choose_borrowed else fresh("selected conditional", True)


def scope() -> None:
    borrowed = Tracked("borrowed", True)
    result = selected_fresh_or(borrowed)
    assert result.label == "selected left"
    del result
    gc.collect()
    result = discarded_fresh_or(borrowed)
    assert result is borrowed
    del result
    gc.collect()
    result = selected_fresh_and(borrowed)
    assert result.label == "selected false"
    del result
    gc.collect()
    result = discarded_fresh_and(borrowed)
    assert result is borrowed
    del result
    gc.collect()
    assert borrowed_or_fresh(borrowed) is borrowed
    result = borrowed_and_fresh(borrowed)
    assert result.label == "selected and"
    del result
    gc.collect()
    assert conditional(borrowed, True) is borrowed
    result = conditional(borrowed, False)
    assert result.label == "selected conditional"
    del result
    gc.collect()
    print("leaving scope")


def main() -> None:
    scope()
    gc.collect()
    print("FINALIZER_JOINS_OK")


main()
"""


TRUTH_REBIND = r"""import gc


active = None


class Rebinding:
    def __init__(self, label: str) -> None:
        self.label = label

    def __bool__(self) -> bool:
        global active
        print("truth " + self.label)
        active = None
        gc.collect()
        return True

    def __del__(self) -> None:
        print("finalize " + self.label)


def select():
    global active
    active = Rebinding("selected")
    return active or Rebinding("not selected")


def main() -> None:
    selected = select()
    assert selected.label == "selected"
    assert active is None
    print("selected alive")
    del selected
    gc.collect()
    print("TRUTH_REBIND_OK")


main()
"""


PROGRAMS = {
    'local_or': (
        LOCAL_OR,
        '4611686018427387911\n'
        '4611686018427387928\n'
    ),
    'integer_boolean_joins': (
        INTEGER_BOOLEAN_JOINS,
        'and left\n'
        'or right\n'
        'or left\n'
        'and right\n'
        'and left\n'
        'INTEGER_BOOLEAN_JOINS_OK\n'
    ),
    'object_assignment_joins': (
        OBJECT_ASSIGNMENT_JOINS,
        'truth discarded or\n'
        'finalize discarded or\n'
        'truth discarded and\n'
        'finalize discarded and\n'
        'truth borrowed\n'
        'finalize selected new\n'
        'finalize borrowed\n'
        'OBJECT_ASSIGNMENT_JOINS_OK\n'
    ),
    'global_assignment_join': (
        GLOBAL_ASSIGNMENT_JOIN,
        'finalize selected\n'
        'finalize fresh selected\n'
        'GLOBAL_ASSIGNMENT_JOIN_OK\n'
    ),
    'container_assignment_join': (
        CONTAINER_ASSIGNMENT_JOIN,
        'finalize borrowed selected\n'
        'finalize fresh selected\n'
        'finalize unselected\n'
        'CONTAINER_ASSIGNMENT_JOIN_OK\n'
    ),
    'finalizer_joins': (
        FINALIZER_JOINS,
        'make selected left\n'
        'truth selected left\n'
        'finalize selected left\n'
        'make discarded left\n'
        'truth discarded left\n'
        'finalize discarded left\n'
        'make selected false\n'
        'truth selected false\n'
        'finalize selected false\n'
        'make discarded true\n'
        'truth discarded true\n'
        'finalize discarded true\n'
        'truth borrowed\n'
        'truth borrowed\n'
        'make selected and\n'
        'finalize selected and\n'
        'make selected conditional\n'
        'finalize selected conditional\n'
        'leaving scope\n'
        'finalize borrowed\n'
        'FINALIZER_JOINS_OK\n'
    ),
    'truth_rebind': (
        TRUTH_REBIND,
        'truth selected\n'
        'selected alive\n'
        'finalize selected\n'
        'TRUTH_REBIND_OK\n'
    ),
}


@pytest.mark.parametrize("case", tuple(PROGRAMS))
def test_boolean_ownership_reference(case, tmp_path):
    program, expected = PROGRAMS[case]
    assert_reference_program(program, expected, tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("case", tuple(PROGRAMS))
def test_boolean_ownership_native_five_gc(
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
    )
