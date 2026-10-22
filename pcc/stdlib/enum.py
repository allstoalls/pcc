"""pcc.stdlib.enum — minimal ``Enum`` / ``IntEnum`` skeleton.
"""
from __future__ import annotations


class _AutoValue:
    pass


def auto():
    return _AutoValue()


class _EnumMeta(type):
    def __new__(mcs, name, bases, ns):
        next_auto = 1
        cls = super().__new__(mcs, name, bases, ns)
        members: dict = {}
        value_to_member: dict = {}
        for k, v in list(ns.items()):
            if k.startswith("_") or callable(v) or isinstance(v, (staticmethod, classmethod, property)):
                continue
            if isinstance(v, _AutoValue):
                v = next_auto
            next_auto = next_auto + 1
            if v in value_to_member:
                inst = value_to_member[v]
                setattr(cls, k, inst)
                members[k] = inst
                continue
            if _int_enum_base(bases):
                inst = int.__new__(cls, v)
            else:
                inst = object.__new__(cls)
            inst._name_ = k
            inst._value_ = v
            setattr(cls, k, inst)
            members[k] = inst
            value_to_member[v] = inst
        cls._member_map_ = members
        cls._value2member_map_ = value_to_member
        return cls

    def __iter__(cls):
        return iter(cls._member_map_.values())

    def __len__(cls):
        return len(cls._member_map_)

    def __contains__(cls, item):
        return item in cls._member_map_.values()

    @property
    def __members__(cls):
        return cls._member_map_

    def __call__(cls, value):
        if value in cls._value2member_map_:
            return cls._value2member_map_[value]
        raise ValueError(str(value) + " is not a valid " + cls.__name__)


def _int_enum_base(bases) -> bool:
    for b in bases:
        if b is int:
            return True
        if getattr(b, "__name__", "") == "IntEnum":
            return True
    return False


class Enum(metaclass=_EnumMeta):
    @property
    def name(self) -> str:
        return self._name_

    @property
    def value(self):
        return self._value_

    def __repr__(self) -> str:
        return f"<{type(self).__name__}.{self._name_}: {self._value_!r}>"

    def __str__(self) -> str:
        return type(self).__name__ + "." + self._name_

    def __eq__(self, other) -> bool:
        return self is other

    def __hash__(self) -> int:
        return hash(self._name_)


class IntEnum(int, Enum):
    def __str__(self) -> str:
        return str(int(self))


def unique(cls):
    seen = {}
    for name, member in cls.__members__.items():
        value = member.value
        if value in seen:
            raise ValueError("duplicate values found in " + cls.__name__)
        seen[value] = name
    return cls


class Flag(Enum):
    pass


class IntFlag(IntEnum):
    pass
