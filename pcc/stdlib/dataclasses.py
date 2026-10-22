"""Small pcc-native subset of :mod:`dataclasses`.

The compiler recognizes ``@dataclass`` for pcc's own frozen AST classes at
compile time. This module exists for runtime imports in self-hosted binaries and
for lightweight stdlib tests; it deliberately avoids importing CPython's
``dataclasses`` module so pcc1 can start without libpython.
"""
from __future__ import annotations


class _MissingType:
    pass


MISSING = _MissingType()


class _FieldSpec:
    def __init__(
        self,
        default=MISSING,
        default_factory=MISSING,
        init=True,
        repr=True,
        compare=True,
        kw_only=False,
    ):
        self.default = default
        self.default_factory = default_factory
        self.init = init
        self.repr = repr
        self.compare = compare
        self.kw_only = kw_only


class _Field:
    def __init__(
        self,
        name,
        default=MISSING,
        default_factory=MISSING,
        init=True,
        repr=True,
        compare=True,
        kw_only=False,
    ):
        self.name = name
        self.default = default
        self.default_factory = default_factory
        self.init = init
        self.repr = repr
        self.compare = compare
        self.kw_only = kw_only


def field(
    default=MISSING,
    default_factory=MISSING,
    init=True,
    repr=True,
    compare=True,
    kw_only=False,
):
    return _FieldSpec(default, default_factory, init, repr, compare, kw_only)


class _DataclassFactory:
    def __init__(
        self,
        init=True,
        repr=True,
        eq=True,
        frozen=False,
        kw_only=False,
        slots=False,
        order=False,
        unsafe_hash=False,
    ):
        self.init = init
        self.repr = repr
        self.eq = eq
        self.frozen = frozen
        self.kw_only = kw_only
        self.slots = slots
        self.order = order
        self.unsafe_hash = unsafe_hash

    def __call__(self, cls):
        return dataclass(
            cls,
            init=self.init,
            repr=self.repr,
            eq=self.eq,
            frozen=self.frozen,
            kw_only=self.kw_only,
            slots=self.slots,
            order=self.order,
            unsafe_hash=self.unsafe_hash,
        )


def _class_fields(cls):
    annotations = getattr(cls, "__annotations__", {})
    out = {}
    for name in annotations:
        default = getattr(cls, name, MISSING)
        default_factory = MISSING
        init = True
        repr_flag = True
        compare = True
        kw_only = False
        if isinstance(default, _FieldSpec):
            spec = default
            default = spec.default
            default_factory = spec.default_factory
            init = spec.init
            repr_flag = spec.repr
            compare = spec.compare
            kw_only = spec.kw_only
            if default is not MISSING:
                setattr(cls, name, default)
        out[name] = _Field(
            name,
            default,
            default_factory,
            init,
            repr_flag,
            compare,
            kw_only,
        )
    return out


def dataclass(
    cls=None,
    init=True,
    repr=True,
    eq=True,
    frozen=False,
    kw_only=False,
    slots=False,
    order=False,
    unsafe_hash=False,
):
    if cls is None:
        return _DataclassFactory(
            init=init,
            repr=repr,
            eq=eq,
            frozen=frozen,
            kw_only=kw_only,
            slots=slots,
            order=order,
            unsafe_hash=unsafe_hash,
        )

    field_map = _class_fields(cls)
    setattr(cls, "__dataclass_fields__", field_map)
    field_items = list(field_map.values())

    if init:
        def __init__(self, *args, **kwargs):
            arg_index = 0
            for item in field_items:
                if not item.init:
                    continue
                if arg_index < len(args):
                    value = args[arg_index]
                    arg_index += 1
                elif item.name in kwargs:
                    value = kwargs[item.name]
                elif item.default_factory is not MISSING:
                    value = item.default_factory()
                elif item.default is not MISSING:
                    value = item.default
                else:
                    raise TypeError("missing required dataclass field: " + item.name)
                setattr(self, item.name, value)

        setattr(cls, "__init__", __init__)

    if repr:
        def __repr__(self):
            parts = []
            for item in field_items:
                if item.repr:
                    parts.append(item.name + "=" + str(getattr(self, item.name)))
            return type(self).__name__ + "(" + ", ".join(parts) + ")"

        setattr(cls, "__repr__", __repr__)

    if eq:
        def __eq__(self, other):
            if type(self) is not type(other):
                return False
            for item in field_items:
                if item.compare and getattr(self, item.name) != getattr(other, item.name):
                    return False
            return True

        setattr(cls, "__eq__", __eq__)

    if unsafe_hash or frozen:
        def __hash__(self):
            values = []
            for item in field_items:
                if item.compare:
                    values.append(getattr(self, item.name))
            return hash(tuple(values))

        setattr(cls, "__hash__", __hash__)

    return cls


def fields(class_or_instance):
    raw = getattr(class_or_instance, "__dataclass_fields__", None)
    if raw is None:
        raw = getattr(type(class_or_instance), "__dataclass_fields__", None)
    if raw is None:
        return ()
    return tuple(raw.values())


def is_dataclass(obj):
    if getattr(obj, "__dataclass_fields__", None) is not None:
        return True
    return getattr(type(obj), "__dataclass_fields__", None) is not None


def asdict(obj):
    out = {}
    for item in fields(obj):
        out[item.name] = getattr(obj, item.name)
    return out


def astuple(obj):
    out = []
    for item in fields(obj):
        out.append(getattr(obj, item.name))
    return tuple(out)


def replace(obj, **changes):
    cls = type(obj)
    kwargs = {}
    for item in fields(obj):
        if item.name in changes:
            kwargs[item.name] = changes[item.name]
        else:
            kwargs[item.name] = getattr(obj, item.name)
    return cls(**kwargs)


def make_dataclass(cls_name: str, fields, **kwargs):
    annotations = {}
    namespace = {"__annotations__": annotations}
    for item in fields:
        name = item[0]
        annotations[name] = item[1]
        if len(item) > 2:
            namespace[name] = item[2]
    cls = type(cls_name, (), namespace)
    return dataclass(
        cls,
        init=kwargs.get("init", True),
        repr=kwargs.get("repr", True),
        eq=kwargs.get("eq", True),
        frozen=kwargs.get("frozen", False),
        kw_only=kwargs.get("kw_only", False),
        slots=kwargs.get("slots", False),
        order=kwargs.get("order", False),
        unsafe_hash=kwargs.get("unsafe_hash", False),
    )
