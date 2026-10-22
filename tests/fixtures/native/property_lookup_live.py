"""Mutable property descriptors keep ordinary Python attribute semantics."""
import time


class Record:
    def __init__(self, value):
        self._value = value

    @property
    def value(self):
        return self._value

    def read(self):
        return self.value


class Child(Record):
    pass


def hinted_read(value: Record):
    return value.value


def inherited_read(value: Child):
    return value.value


def replacement_year(value):
    return 1776


def failing_getter(value):
    raise ValueError("replacement getter")


def main():
    record = Record(("original",))
    child = Child(("child",))
    original = Record.value
    assert record.value == ("original",)
    assert record.read() == ("original",)
    assert hinted_read(record) == ("original",)
    assert inherited_read(child) == ("child",)
    Record.value = property(replacement_year)
    assert record.value == 1776
    assert record.read() == 1776
    assert hinted_read(record) == 1776
    assert inherited_read(child) == 1776
    Record.value = 42
    assert record.value == 42
    assert record.read() == 42
    assert hinted_read(record) == 42
    assert inherited_read(child) == 42
    Record.value = property(failing_getter)
    try:
        hinted_read(record)
    except ValueError as error:
        assert str(error) == "replacement getter"
    else:
        raise AssertionError("replacement exception lost")
    Record.value = original
    assert record.value == ("original",)
    assert record.read() == ("original",)
    assert hinted_read(record) == ("original",)
    assert inherited_read(child) == ("child",)

    # Preserve the original time acceptance assertion and tuple contents.
    values = (2024, 2, 29, 12, 34, 56, 3, 60, 0)
    record = time.struct_time(sequence=values)
    original_year = time.struct_time.tm_year
    time.struct_time.tm_year = property(replacement_year)
    assert record.tm_year == 1776
    assert tuple(record) == values
    time.struct_time.tm_year = original_year
    assert record.tm_year == 2024
    print("LIVE_PROPERTY_LOOKUP_OK")


main()
