"""Required typed-property semantics, unresolved by the DynType-only repair.

Annotations do not prevent replacing a property or changing its result type.
Keep these assertions even while the native implementation cannot satisfy them.
"""


class IntegerRecord:
    @property
    def value(self) -> int:
        return 1

    def read(self):
        return self.value


class IntegerChild(IntegerRecord):
    pass


class FloatRecord:
    @property
    def value(self) -> float:
        return 1.0

    def read(self):
        return self.value


class FloatChild(FloatRecord):
    pass


def int_hint(record: IntegerRecord):
    return record.value


def int_inherited(record: IntegerChild):
    return record.value


def float_hint(record: FloatRecord):
    return record.value


def float_inherited(record: FloatChild):
    return record.value


def large_integer(record):
    return 1 << 100


def tuple_result(record):
    return ("replacement",)


def string_result(record):
    return "replacement"


def check_int(record: IntegerRecord, child: IntegerChild, expected):
    assert record.value == expected
    assert record.read() == expected
    assert int_hint(record) == expected
    assert child.value == expected
    assert child.read() == expected
    assert int_inherited(child) == expected
    assert type(record.value) is type(expected)
    assert type(int_hint(record)) is type(expected)


def check_float(record: FloatRecord, child: FloatChild, expected):
    assert record.value == expected
    assert record.read() == expected
    assert float_hint(record) == expected
    assert child.value == expected
    assert child.read() == expected
    assert float_inherited(child) == expected
    assert type(record.value) is type(expected)
    assert type(float_hint(record)) is type(expected)


def main():
    integer, integer_child = IntegerRecord(), IntegerChild()
    floating, float_child = FloatRecord(), FloatChild()
    original_int, original_float = IntegerRecord.value, FloatRecord.value
    check_int(integer, integer_child, 1)
    check_float(floating, float_child, 1.0)
    for descriptor, expected in (
        (property(large_integer), 1 << 100),
        (property(tuple_result), ("replacement",)),
        (property(string_result), "replacement"),
        (1 << 100, 1 << 100),
        (("replacement",), ("replacement",)),
        ("replacement", "replacement"),
    ):
        IntegerRecord.value = descriptor
        FloatRecord.value = descriptor
        check_int(integer, integer_child, expected)
        check_float(floating, float_child, expected)
    IntegerRecord.value, FloatRecord.value = original_int, original_float
    check_int(integer, integer_child, 1)
    check_float(floating, float_child, 1.0)
    print("LIVE_PROPERTY_LOOKUP_OK")


main()
