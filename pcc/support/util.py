_contracts = {}


class Contract:
    @classmethod
    def __init_subclass__(cls):
        _contracts[cls.__name__] = cls

    @classmethod
    def check(cls, value):
        pass

    def __set__(self, instance, value):
        self.check(value)
        instance.__dict__[self.name] = value

    def __set_name__(self, owner, name):
        self.name = name


class Typed(Contract):
    type = None

    @classmethod
    def check(cls, value):
        if not isinstance(value, cls.type):
            raise TypeError(f"Expected {cls.type}, got {type(value).__name__}")


class Integer(Typed):
    type = int

    @classmethod
    def check(cls, value):
        if not isinstance(value, int):
            raise TypeError(f"Expected int, got {type(value).__name__}")
        super().check(value)


class Positive(Typed):
    @classmethod
    def check(cls, value):
        if not value > 0:
            raise ValueError(f"Must be > 0, got {value}")
        super().check(value)


class PositiveInteger(Integer, Positive):
    pass


from functools import wraps
from inspect import signature


def checked(func):
    sig = signature(func)
    ann = ChainMap(func.__annotations__, func.__globals__.get("__annotations__", {}))

    @wraps(func)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        for name, val in bound.arguments.items():
            if name in ann:
                ann[name].check(val)
        return func(*args, **kwargs)

    return wrapper


from collections import ChainMap


def _class_annotations(cls):
    return getattr(cls, "__annotations__", {})


class BaseMeta(type):
    @classmethod
    def __prepare__(cls, *args):
        return ChainMap({}, _contracts)

    def __new__(meta, name, bases, methods):
        methods = methods.maps[0]
        return super().__new__(meta, name, bases, methods)


class Base(metaclass=BaseMeta):
    @classmethod
    def __init_subclass__(cls):
        for name, val in cls.__dict__.items():
            if callable(val):
                setattr(cls, name, checked(val))

        for name, val in _class_annotations(cls).items():
            contract = val()
            contract.__set_name__(cls, name)
            setattr(cls, name, contract)

    def __init__(self, *args):
        ann = _class_annotations(type(self))
        if len(args) != len(ann):
            raise TypeError(
                f"{type(self).__name__} expected {len(ann)} arguments, got {len(args)}"
            )

        for name, val in zip(ann, args):
            setattr(self, name, val)

    def __repr__(self):
        args = ",".join(
            repr(getattr(self, name)) for name in _class_annotations(type(self))
        )
        return f"{type(self).__name__}({args})"
