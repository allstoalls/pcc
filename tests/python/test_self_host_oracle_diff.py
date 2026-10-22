from __future__ import annotations

from tests.owned_ir_validation import verify_ir_text

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path

import pytest

from pcc.diagnostics.macho_normalize import normalize_macho_metadata
from tests.host_pcc_pcc1_parity import (
    ParityContractError,
    load_applicability_manifest,
    pcc1_receipt_path,
    verify_pcc1_receipt,
    write_parity_failure_report,
    write_pcc1_receipt,
)
from tests.py_corpus_support import PYTHON_CORPUS_CASES, PythonCorpusCase
from tests.python import test_intent_constraints as intent_constraints
from tests.runtime_build_cache import (
    cached_self_host_oracle_dir,
    self_backend_object_cache_key,
    self_host_object_cache_dir,
    self_host_source_key,
)

# The pcc1/pcc2/pcc3 fixtures are session-scoped per xdist worker but publish
# one source-addressed stage chain.  Keep every dependent oracle on the bounded
# self-heavy lane so one worker owns the cold build; otherwise several workers
# can retry the same failed 600-second stage and turn one warmup timeout into
# hundreds of cached fixture errors.
pytestmark = pytest.mark.xdist_group(name="pcc_heavy_self")

REPO_ROOT = Path(__file__).absolute().parents[2]
NO_HOST_PYTHON = shutil.which("false") or "/usr/bin/false"

CASES: tuple[tuple[str, str], ...] = (
    (
        "ternary_value",
        """
        def main() -> None:
            n = 0
            y = 100 if n == 0 else n * 2
            print(y)
            n = 3
            z = 100 if n == 0 else n * 2
            print(z)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "ternary_inline",
        """
        def f(n):
            return 100 if n == 0 else n * 2

        def main() -> None:
            print(f(0))
            print(f(3))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "chained_compare",
        """
        def main() -> None:
            print(1 < 2 < 3)
            print(1 < 3 < 2)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "bitwise_int_ops",
        """
        def combine(a: int, b: int) -> None:
            print(a & b)
            print(a | b)
            print(a ^ b)
            print(~b)
            print(a << 2)
            print(a >> 1)

        def main() -> None:
            left = 6
            right = 3
            combine(left, right)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "bitwise_negative_shift_errors",
        """
        def main() -> None:
            try:
                print(1 << -1)
                print("left-missed")
            except ValueError:
                print("left-error")
            try:
                print(8 >> -2)
                print("right-missed")
            except ValueError:
                print("right-error")

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring",
        """
        def main() -> None:
            n = 7
            print(f"n={n}")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring_format_spec",
        """
        def main() -> None:
            n = 255
            print(f"{n:x}")
            print(f"{n:04x}")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring_debug_and_conversions",
        """
        def main() -> None:
            n = 3
            text = "hi"
            print(f"{n + 4}")
            print(f"{n=}")
            print(f"{text!r}")
            print(f"{text!s}")
            print(f"{{{text}}}")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring_attr_call_mix",
        """
        class Box:
            def __init__(self, name: str) -> None:
                self.name = name

            def label(self) -> str:
                return self.name + "!"

        def main() -> None:
            b = Box("core")
            print(f"{b.name}:{len(b.name)}")
            print(f"{b.label()}")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring_dynamic_format_spec",
        """
        def main() -> None:
            n = 255
            width = 4
            print(f"{n:0{width}x}")
            print(f"{n:{width}}")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring_custom_format",
        """
        class Box:
            def __init__(self, name: str) -> None:
                self.name = name

            def __format__(self, spec: str) -> str:
                return self.name + ":" + spec

        def main() -> None:
            b = Box("core")
            print(f"{b:tag}")
            print(format(b, "raw"))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "string_concat_runtime",
        """
        def pair(a: str, b: str) -> str:
            return a + ":" + b

        def main() -> None:
            left = "ab"
            right = "cd"
            print(left + right)
            print(pair("left", "right"))
            left = left + "!"
            print(left)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "fstring_ascii_conversion_non_ascii",
        """
        def main() -> None:
            value = "\\u00e9"
            face = chr(0x1f600)
            print(f"{value!a}")
            print(f"{face!a}")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "generator_fstring_join",
        """
        class Arg:
            def __init__(self, type: str, text: str) -> None:
                self.type = type
                self.text = text

            def __str__(self) -> str:
                return self.text

        def main() -> None:
            args = [Arg("i64", "%a"), Arg("ptr", "%b")]
            print(", ".join(f"{a.type} {a}" for a in args))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "tuple_generator_expression",
        """
        def main() -> None:
            values = [1, 2, 3]
            out = tuple(x + 10 for x in values)
            print(out[0])
            print(out[2])
            print(len(out))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "walrus_expression",
        """
        def main() -> None:
            values = [1, 2, 3]
            if (n := len(values)) > 2:
                print(n)
            print(n + 1)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "int_builtin",
        """
        def main() -> None:
            print(int("12") + int(True))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_method",
        """
        class Box:
            def __init__(self, value):
                self.value = value
            def get(self):
                return self.value

        def main() -> None:
            b = Box(9)
            print(b.get())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "custom_getattribute",
        """
        class Probe:
            def __init__(self) -> None:
                self.raw = "stored"

            def __getattribute__(self, name: str):
                if name == "label":
                    return "intercepted"
                return object.__getattribute__(self, name)

        def main() -> None:
            p = Probe()
            print(p.label)
            print(p.raw)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "custom_getattribute_getattr_fallback",
        """
        class Probe:
            def __init__(self) -> None:
                self.raw = "stored"

            def __getattribute__(self, name: str):
                if name == "dynamic":
                    raise AttributeError(name)
                return object.__getattribute__(self, name)

            def __getattr__(self, name: str):
                print("getattr:" + name)
                return "fallback:" + name

        def main() -> None:
            p = Probe()
            print(p.dynamic)
            print(p.raw)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "custom_getattribute_valueerror_propagates",
        """
        class Probe:
            def __init__(self) -> None:
                self.raw = "stored"

            def __getattribute__(self, name: str):
                if name == "boom":
                    raise ValueError("boom")
                return object.__getattribute__(self, name)

            def __getattr__(self, name: str):
                print("bad_getattr:" + name)
                return "bad"

        def main() -> None:
            p = Probe()
            try:
                print(p.boom)
            except ValueError:
                print("caught_valueerror")
            print(p.raw)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_literal_class_attrs",
        """
        class Meta(type):
            def __new__(mcls, name, bases, ns):
                ns["marker"] = "ok"
                ns["count"] = 3
                return type.__new__(mcls, name, bases, ns)

        class Host(metaclass=Meta):
            pass

        def main() -> None:
            print(Host.marker)
            print(Host.count + 4)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_method_binding",
        """
        class Meta(type):
            def label(cls):
                return "meta:" + cls.__name__

        class Host(metaclass=Meta):
            pass

        def main() -> None:
            print(Host.label())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_property_binding",
        """
        class Meta(type):
            @property
            def label(cls):
                return "meta:" + cls.__name__

        class Host(metaclass=Meta):
            pass

        def main() -> None:
            print(Host.label)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_property_readonly_errors",
        """
        class Meta(type):
            @property
            def label(cls):
                return "meta:" + cls.__name__

        class Host(metaclass=Meta):
            pass

        def main() -> None:
            print(Host.label)
            try:
                Host.label = "next"
                print("set-missed")
            except AttributeError:
                print("set-error")
            try:
                del Host.label
                print("del-missed")
            except AttributeError:
                print("del-error")
            print(Host.label)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_property_readonly_precedence",
        """
        class Meta(type):
            @property
            def label(cls):
                return "meta:" + cls.__name__

        class Host(metaclass=Meta):
            label = "class-label"

        def main() -> None:
            print(Host.label)
            try:
                Host.label = "next"
                print("set-missed")
            except AttributeError:
                print("set-error")
            print(Host.__dict__["label"])
            try:
                del Host.label
                print("del-missed")
            except AttributeError:
                print("del-error")
            print(Host.__dict__["label"])
            print(Host.label)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_custom_descriptor_set_delete",
        """
        class Descriptor:
            def __init__(self) -> None:
                self.last = "unset"

            def __get__(self, obj, owner):
                return "get:" + obj.__name__ + ":" + owner.__name__ + ":" + self.last

            def __set__(self, obj, value) -> None:
                print("set:" + obj.__name__ + ":" + value)
                self.last = value

            def __delete__(self, obj) -> None:
                print("delete:" + obj.__name__)
                self.last = "deleted"

        class Meta(type):
            desc = Descriptor()

        class Host(metaclass=Meta):
            pass

        def main() -> None:
            print(Host.desc)
            Host.desc = "next"
            print(Host.desc)
            del Host.desc
            print(Host.desc)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_data_descriptor_precedence",
        """
        class Descriptor:
            def __get__(self, obj, owner):
                return "meta-get:" + obj.__name__ + ":" + owner.__name__

            def __set__(self, obj, value) -> None:
                print("meta-set:" + obj.__name__ + ":" + value)

            def __delete__(self, obj) -> None:
                print("meta-delete:" + obj.__name__)

        class Meta(type):
            label = Descriptor()

        class Host(metaclass=Meta):
            label = "class-label"

        def main() -> None:
            print(Host.label)
            Host.label = "next"
            print(Host.__dict__["label"])
            del Host.label
            print(Host.__dict__["label"])
            print(Host.label)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_runtime_class_object_property_precedence",
        """
        class Meta(type):
            @property
            def label(cls):
                return "meta:" + cls.__name__

        class Host(metaclass=Meta):
            label = "class-label"

        def probe(cls) -> None:
            print(cls.label)
            try:
                cls.label = "next"
                print("set-missed")
            except AttributeError:
                print("set-error")
            print(cls.__dict__["label"])
            try:
                del cls.label
                print("del-missed")
            except AttributeError:
                print("del-error")
            print(cls.__dict__["label"])
            print(cls.label)

        def main() -> None:
            probe(Host)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_runtime_class_object_data_descriptor_precedence",
        """
        class Descriptor:
            def __get__(self, obj, owner):
                return "meta-get:" + obj.__name__ + ":" + owner.__name__

            def __set__(self, obj, value) -> None:
                print("meta-set:" + obj.__name__ + ":" + value)

            def __delete__(self, obj) -> None:
                print("meta-delete:" + obj.__name__)

        class Meta(type):
            label = Descriptor()

        class Host(metaclass=Meta):
            label = "class-label"

        def probe(cls) -> None:
            print(cls.label)
            cls.label = "next"
            print(cls.__dict__["label"])
            del cls.label
            print(cls.__dict__["label"])
            print(cls.label)

        def main() -> None:
            probe(Host)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_runtime_class_object_property_setter_deleter_precedence",
        """
        class Meta(type):
            @property
            def label(cls):
                return "meta:" + cls.state

            @label.setter
            def label(cls, value) -> None:
                print("set:" + cls.__name__ + ":" + value)
                cls.state = value

            @label.deleter
            def label(cls) -> None:
                print("delete:" + cls.__name__)
                cls.state = "deleted"

        class Host(metaclass=Meta):
            state = "start"
            label = "class-label"

        def probe(cls) -> None:
            print(cls.label)
            cls.label = "next"
            print(cls.label)
            print(cls.__dict__["label"])
            del cls.label
            print(cls.label)
            print(cls.__dict__["label"])

        def main() -> None:
            probe(Host)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_namespace",
        """
        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return {"marker": "prepared", "count": 4}

        class Host(metaclass=Meta):
            pass

        def main() -> None:
            print(Host.marker)
            print(Host.count + 3)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_namespace_body_override",
        """
        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return {"marker": "prepared", "count": 4}

        class Host(metaclass=Meta):
            marker = "body"
            count = 9

        def main() -> None:
            print(Host.marker)
            print(Host.count + 1)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_namespace_body_extends",
        """
        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return {"marker": "prepared"}

        class Host(metaclass=Meta):
            count = 9

        def main() -> None:
            print(Host.marker)
            print(Host.count + 1)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_namespace_method_and_body",
        """
        def prepared_method(self):
            return "prepared:" + self.label

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return {"prepared": prepared_method}

        class Host(metaclass=Meta):
            label = "body"

            def body(self):
                return self.prepared() + ":body"

        def main() -> None:
            obj = Host()
            print(obj.prepared())
            print(obj.body())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_namespace_non_mapping_typeerror",
        """
        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return 42

        try:
            class Host(metaclass=Meta):
                value = 1
        except TypeError:
            RESULT = "type-error"
        else:
            RESULT = "no-error"

        def main() -> None:
            print(RESULT)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_setitem_order",
        """
        LOG = []

        class Namespace:
            def __init__(self):
                self.data = {}
                self.log = []

            def __setitem__(self, key, value):
                self.log.append(key)
                self.data[key] = value

            def __getitem__(self, key):
                return self.data[key]

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return Namespace()

            def __new__(mcls, name, bases, ns):
                LOG.append("|".join(ns.log))
                return type.__new__(mcls, name, bases, ns.data)

        class Host(metaclass=Meta):
            value = "body"

            def method(self):
                return self.value

        def main() -> None:
            obj = Host()
            print(LOG[0])
            print(obj.method())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_getitem_new",
        """
        LOG = []

        class Namespace:
            def __init__(self):
                self.data = {}
                self.log = []

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return Namespace()

            def __new__(mcls, name, bases, ns):
                value = ns["value"]
                LOG.append(ns.log[len(ns.log) - 1])
                cls = type.__new__(mcls, name, bases, ns.data)
                cls.copied = value
                return cls

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(Host.copied)
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_class_body_lookup",
        """
        LOG = []

        class Namespace:
            def __init__(self):
                self.data = {}
                self.log = []

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return Namespace()

            def __new__(mcls, name, bases, ns):
                LOG.append("|".join(ns.log))
                return type.__new__(mcls, name, bases, ns.data)

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_constructor_args",
        """
        LOG = []

        class Namespace:
            def __init__(self, tag):
                self.data = {}
                self.log = ["init:" + tag]

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return Namespace("tag")

            def __new__(mcls, name, bases, ns):
                value = ns["value"]
                LOG.append("|".join(ns.log))
                cls = type.__new__(mcls, name, bases, ns.data)
                cls.copied = value
                return cls

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(Host.copied)
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_factory_return",
        """
        LOG = []

        class Namespace:
            def __init__(self, tag):
                self.data = {}
                self.log = ["init:" + tag]

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        def make_namespace(tag):
            LOG.append("factory:" + tag)
            return Namespace(tag)

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return make_namespace("tag")

            def __new__(mcls, name, bases, ns):
                value = ns["value"]
                LOG.append("|".join(ns.log))
                cls = type.__new__(mcls, name, bases, ns.data)
                cls.copied = value
                return cls

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(Host.copied)
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_alias_constructor",
        """
        LOG = []

        class Namespace:
            def __init__(self, tag):
                self.data = {}
                self.log = ["init:" + tag]

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        Ns = Namespace

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return Ns("tag")

            def __new__(mcls, name, bases, ns):
                value = ns["value"]
                LOG.append("|".join(ns.log))
                cls = type.__new__(mcls, name, bases, ns.data)
                cls.copied = value
                return cls

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(Host.copied)
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_factory_local_return",
        """
        LOG = []

        class Namespace:
            def __init__(self, tag):
                self.data = {}
                self.log = ["init:" + tag]

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        def make_namespace(tag):
            LOG.append("factory:" + tag)
            ns = Namespace(tag)
            return ns

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return make_namespace("tag")

            def __new__(mcls, name, bases, ns):
                value = ns["value"]
                LOG.append("|".join(ns.log))
                cls = type.__new__(mcls, name, bases, ns.data)
                cls.copied = value
                return cls

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(Host.copied)
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_generic_mapping_factory",
        """
        LOG = []

        class Namespace:
            def __init__(self, tag):
                self.data = {}
                self.log = ["init:" + tag]

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        def make_namespace(tag):
            LOG.append("factory:" + tag)
            ns = Namespace(tag)
            if tag == "tag":
                return ns
            return ns

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return make_namespace("tag")

            def __new__(mcls, name, bases, ns):
                value = ns["value"]
                LOG.append("|".join(ns.log))
                cls = type.__new__(mcls, name, bases, ns.data)
                cls.copied = value
                return cls

        class Host(metaclass=Meta):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(Host.copied)
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_custom_namespace_delete_name",
        """
        LOG = []

        class Namespace:
            def __init__(self):
                self.data = {}
                self.log = []

            def __setitem__(self, key, value):
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

            def __delitem__(self, key):
                self.log.append("del:" + key)
                del self.data[key]

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return Namespace()

            def __new__(mcls, name, bases, ns):
                LOG.append("|".join(ns.log))
                return type.__new__(mcls, name, bases, ns.data)

        class Host(metaclass=Meta):
            value = "body"
            seen = value
            del value

        def main() -> None:
            print(LOG[0])
            print(Host.seen)
            try:
                print(Host.value)
            except AttributeError:
                print("value-missing")

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_keyword_arguments_prepare_new",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases, **kwargs):
                LOG.append("prepare:" + kwargs["tag"])
                return {}

            def __new__(mcls, name, bases, ns, **kwargs):
                LOG.append("new:" + kwargs["tag"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.tag = kwargs["tag"]
                return cls

        class Host(metaclass=Meta, tag="ready"):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(Host.tag + ":" + Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_call_controls_instantiation",
        """
        LOG = []

        class Meta(type):
            def __call__(cls, label):
                LOG.append(cls.kind)
                return "made:" + cls.kind + ":" + label

        class Host(metaclass=Meta):
            kind = "HostKind"

        def main() -> None:
            result = Host("ready")
            print(LOG[0])
            print(result)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_call_delegates_type_call",
        """
        LOG = []

        class Meta(type):
            def __call__(cls, label):
                LOG.append("call:" + label)
                obj = type.__call__(cls, label + ":init")
                obj.seen = obj.seen + ":call"
                return obj

        class Host(metaclass=Meta):
            def __init__(self, label):
                self.seen = label

        def main() -> None:
            obj = Host("ready")
            print(LOG[0])
            print(obj.seen)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_binding",
        """
        class Meta(type):
            def label(cls):
                return "meta:" + cls.__name__

        chosen = Meta

        class Host(metaclass=chosen):
            value = "body"

        def main() -> None:
            print(Host.label())
            print(Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_exception_propagates",
        """
        def choose_meta():
            raise ValueError("meta-boom")

        try:
            class Host(metaclass=choose_meta()):
                value = "body"
        except ValueError as exc:
            RESULT = "caught:" + str(exc)
        else:
            RESULT = "no-error"

        def main() -> None:
            print(RESULT)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_function_return",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name)
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "chosen"
                return cls

        def choose_meta():
            LOG.append("choose")
            return Meta

        class Host(metaclass=choose_meta()):
            value = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(Host.origin + ":" + Host.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_function_arg_return",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        def choose(tag):
            LOG.append("choose:" + tag)
            return Meta

        class Host(metaclass=choose("arg")):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_conditional_return",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        def choose(tag):
            LOG.append("choose:" + tag)
            if tag == "arg":
                return Meta
            else:
                return Meta

        class Host(metaclass=choose("arg")):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_conditional_expr",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        def choose():
            LOG.append("choose")
            return False

        def select_then():
            LOG.append("then")
            return Meta

        def select_else():
            LOG.append("else")
            return Meta

        class Host(metaclass=select_then() if choose() else select_else()):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(LOG[3])
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_bool_or_expr",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        def choose():
            LOG.append("choose")
            return Meta

        def fallback():
            LOG.append("fallback")
            return Meta

        class Host(metaclass=choose() or fallback()):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(len(LOG))
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_bool_and_expr",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        def choose():
            LOG.append("choose")
            return Meta

        def fallback():
            LOG.append("fallback")
            return Meta

        class Host(metaclass=choose() and fallback()):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(LOG[3])
            print(len(LOG))
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_bool_or_falsey_left_expr",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        def choose_none():
            LOG.append("choose-none")
            return None

        def fallback():
            LOG.append("fallback")
            return Meta

        class Host(metaclass=choose_none() or fallback()):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(LOG[3])
            print(len(LOG))
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_bool_or_alias_fallback",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        AliasMeta = Meta

        def choose_none():
            LOG.append("choose-none")
            return None

        class Host(metaclass=choose_none() or AliasMeta):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(len(LOG))
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_dynamic_value_bool_and_or_falsey_chain",
        """
        LOG = []

        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                LOG.append("prepare:" + name)
                return {}

            def __new__(mcls, name, bases, ns):
                LOG.append("new:" + name + ":" + ns["kind"])
                cls = type.__new__(mcls, name, bases, ns)
                cls.origin = "meta"
                return cls

        AliasMeta = Meta

        def choose_none():
            LOG.append("choose-none")
            return None

        def fallback():
            LOG.append("fallback")
            return Meta

        class Host(metaclass=choose_none() and AliasMeta or fallback()):
            kind = "body"

        def main() -> None:
            print(LOG[0])
            print(LOG[1])
            print(LOG[2])
            print(LOG[3])
            print(len(LOG))
            print(Host.origin + ":" + Host.kind)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_prepare_namespace_non_string_key_typeerror",
        """
        class Meta(type):
            @classmethod
            def __prepare__(mcls, name, bases):
                return {1: "bad"}

        try:
            class Host(metaclass=Meta):
                value = "body"
        except TypeError:
            RESULT = "type-error"
        else:
            RESULT = "no-error"

        def main() -> None:
            print(RESULT)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_inherited_from_base",
        """
        class Meta(type):
            def label(cls):
                return "meta:" + cls.__name__

        class Base(metaclass=Meta):
            base = "base"

        class Child(Base):
            value = "child"

        def main() -> None:
            print(Child.label())
            print(Child.base + ":" + Child.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_conflict_between_bases_typeerror",
        """
        class MetaA(type):
            pass

        class MetaB(type):
            pass

        class A(metaclass=MetaA):
            pass

        class B(metaclass=MetaB):
            pass

        try:
            class Bad(A, B):
                pass
        except TypeError:
            RESULT = "type-error"
        else:
            RESULT = "no-error"

        def main() -> None:
            print(RESULT)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "metaclass_compatible_bases_choose_most_derived",
        """
        class MetaA(type):
            def label(cls):
                return "A:" + cls.__name__

        class MetaB(MetaA):
            def label(cls):
                return "B:" + cls.__name__

        class A(metaclass=MetaA):
            a = "a"

        class B(metaclass=MetaB):
            b = "b"

        class Good(A, B):
            value = "good"

        def main() -> None:
            print(Good.label())
            print(Good.a + Good.b + Good.value)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_class_attrs",
        """
        class Base:
            def value(self):
                return 5

        def main() -> None:
            Dynamic = type("Dynamic", (Base,), {"marker": "ok", "extra": 7})
            print(Dynamic.marker)
            print(Dynamic.extra + 1)
            print(Dynamic().value())
            print(isinstance(Dynamic(), Base))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_var",
        """
        class Base:
            def value(self):
                return 5

        def main() -> None:
            ns = {"marker": "ok", "extra": 7}
            Dynamic = type("Dynamic", (Base,), ns)
            ns["marker"] = "changed"
            print(Dynamic.marker)
            print(Dynamic.extra + Dynamic().value())
            print(isinstance(Dynamic(), Base))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_alias",
        """
        class Base:
            def value(self):
                return 5

        def main() -> None:
            ns = {"marker": "ok", "extra": 7}
            alias = ns
            Dynamic = type("Dynamic", (Base,), alias)
            print(Dynamic.marker)
            print(Dynamic.extra + Dynamic().value())
            print(isinstance(Dynamic(), Base))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_mutation_before_call",
        """
        class Base:
            def value(self):
                return 5

        def main() -> None:
            ns = {}
            ns["marker"] = "ok"
            ns["extra"] = 7
            Dynamic = type("Dynamic", (Base,), ns)
            print(Dynamic.marker)
            print(Dynamic.extra + Dynamic().value())
            print(isinstance(Dynamic(), Base))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_runtime_namespace_dict",
        """
        class Base:
            def value(self):
                return 5

        def make_ns():
            ns = {}
            ns["marker"] = "ok"
            ns["extra"] = 7
            return ns

        def main() -> None:
            ns = make_ns()
            Dynamic = type("Dynamic", (Base,), ns)
            print(Dynamic.marker)
            print(Dynamic.extra + Dynamic().value())
            print(isinstance(Dynamic(), Base))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_mapping_typeerror",
        """
        class MappingOnly:
            def keys(self):
                return ["marker"]

            def __getitem__(self, key):
                return "bad"

        def main() -> None:
            try:
                type("Dynamic", (), MappingOnly())
                print("no-error")
            except Exception:
                print("type-error")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_function_method",
        """
        def label(self):
            return "method-ok"

        def main() -> None:
            Dynamic = type("Dynamic", (), {"label": label})
            print(Dynamic().label())
            print(Dynamic.label(Dynamic()))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_descriptor",
        """
        class Descriptor:
            def __get__(self, obj, owner):
                if obj is None:
                    return "dynamic-class"
                return "dynamic-inst"

        def main() -> None:
            Dynamic = type("Dynamic", (), {"desc": Descriptor()})
            print(Dynamic.desc)
            print(Dynamic().desc)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_staticmethod",
        """
        def label():
            return "static-ok"

        def main() -> None:
            Dynamic = type("Dynamic", (), {"label": staticmethod(label)})
            print(Dynamic.label())
            print(Dynamic().label())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_classmethod",
        """
        def label(cls):
            return cls.__name__

        def main() -> None:
            Dynamic = type("Dynamic", (), {"label": classmethod(label)})
            print(Dynamic.label())
            print(Dynamic().label())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_data_descriptor",
        """
        class DataDescriptor:
            def __init__(self) -> None:
                self.last = "unset"

            def __get__(self, obj, owner):
                return "get:" + self.last

            def __set__(self, obj, value) -> None:
                print("set:" + value)
                self.last = value

            def __delete__(self, obj) -> None:
                print("delete")
                self.last = "deleted"

        def main() -> None:
            Dynamic = type("Dynamic", (), {"desc": DataDescriptor()})
            obj = Dynamic()
            obj.desc = "next"
            print(obj.desc)
            del obj.desc
            print(obj.desc)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_property",
        """
        def read(obj):
            return "get:" + obj.name

        def write(obj, value) -> None:
            print("set:" + value)
            obj.name = value

        def remove(obj) -> None:
            print("delete")
            obj.name = "deleted"

        def main() -> None:
            Dynamic = type("Dynamic", (), {"prop": property(read, write, remove)})
            obj = Dynamic()
            obj.name = "start"
            print(obj.prop)
            obj.prop = "next"
            print(obj.prop)
            del obj.prop
            print(obj.prop)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_type_constructor_namespace_property_readonly_errors",
        """
        def read(obj):
            return "get:" + obj.name

        def main() -> None:
            Dynamic = type("Dynamic", (), {"prop": property(read)})
            obj = Dynamic()
            obj.name = "start"
            print(obj.prop)
            try:
                obj.prop = "next"
                print("set-missed")
            except AttributeError:
                print("set-error")
            try:
                del obj.prop
                print("del-missed")
            except AttributeError:
                print("del-error")
            print(obj.prop)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "property_decorator_get_set_delete",
        """
        class Decorated:
            def __init__(self) -> None:
                self.name = "start"

            @property
            def label(self):
                return "get:" + self.name

            @label.setter
            def label(self, value) -> None:
                print("set:" + value)
                self.name = value

            @label.deleter
            def label(self) -> None:
                print("delete")
                self.name = "deleted"

        def main() -> None:
            obj = Decorated()
            print(obj.label)
            obj.label = "next"
            print(obj.label)
            del obj.label
            print(obj.label)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "descriptor_get",
        """
        class Descriptor:
            def __get__(self, obj, owner):
                if obj is None:
                    return "class"
                return obj.name + ":desc"

        class Host:
            desc = Descriptor()

            def __init__(self) -> None:
                self.name = "inst"

        def main() -> None:
            print(Host.desc)
            print(Host().desc)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "data_descriptor_priority",
        """
        class DataDescriptor:
            def __init__(self) -> None:
                self.last = "unset"

            def __get__(self, obj, owner):
                if obj is None:
                    return "class:" + self.last
                return "get:" + self.last

            def __set__(self, obj, value) -> None:
                print("set:" + value)
                self.last = value

        class Host:
            desc = DataDescriptor()

            def __init__(self) -> None:
                self.desc = "init"

        def main() -> None:
            h = Host()
            print(h.desc)
            h.desc = "next"
            print(h.desc)
            print(Host.desc)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "user_instance_subscript_setitem_getitem",
        """
        class Box:
            def __init__(self) -> None:
                self.data = {}
                self.log = []

            def __setitem__(self, key, value) -> None:
                self.log.append("set:" + key)
                self.data[key] = value

            def __getitem__(self, key):
                self.log.append("get:" + key)
                return self.data[key]

        def main() -> None:
            box = Box()
            box["k"] = "v"
            print(box["k"])
            print("|".join(box.log))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "descriptor_delete",
        """
        class DataDescriptor:
            def __init__(self) -> None:
                self.last = "set"

            def __get__(self, obj, owner):
                if obj is None:
                    return "class:" + self.last
                return "get:" + self.last

            def __delete__(self, obj) -> None:
                print("delete")
                self.last = "deleted"

        class Host:
            desc = DataDescriptor()

        def main() -> None:
            h = Host()
            print(h.desc)
            del h.desc
            print(h.desc)
            print(Host.desc)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_method",
        """
        class Base:
            def label(self):
                return "base"

        class Child(Base):
            def label(self):
                return super().label() + ":child"

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_classmethod",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is Child:
                    return "child"
                return "wrong"

        class Child(Base):
            @classmethod
            def label(cls):
                return super().label() + ":via-child"

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_nested_method_with_receiver",
        """
        class Base:
            def label(self):
                return "base"

        class Child(Base):
            def label(self):
                def inner(self):
                    return super().label()
                return inner(self) + ":child"

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_nested_class_receiver",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is Grand:
                    return "grand"
                if cls is Child:
                    return "child"
                return "base"

        class Child(Base):
            @classmethod
            def label(cls):
                def inner(cls):
                    return super().label()
                return inner(cls) + ":via-child"

        class Grand(Child):
            pass

        def main() -> None:
            print(Grand.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_escaping_nested_class_receiver",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is Grand:
                    return "grand"
                if cls is Child:
                    return "child"
                return "base"

        class Child(Base):
            @classmethod
            def make(cls):
                def inner(cls):
                    return super().label()
                return inner

        class Grand(Child):
            pass

        def main() -> None:
            fn = Grand.make()
            print(fn(Grand))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_escaping_nested_method_receiver",
        """
        class Base:
            def label(self):
                return "base:" + self.name

        class Child(Base):
            def __init__(self, name):
                self.name = name

            def make(self):
                def inner(self):
                    return super().label()
                return inner

        def main() -> None:
            obj = Child("child")
            fn = obj.make()
            print(fn(obj))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dunder_class_cell_method",
        """
        class Base:
            marker = "base"

        class Child(Base):
            marker = "child"

            def label(self):
                cls = __class__
                return cls.__name__ + ":" + cls.marker

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dunder_class_cell_nested_method",
        """
        class Base:
            marker = "base"

        class Child(Base):
            marker = "child"

            def label(self):
                def inner():
                    cls = __class__
                    return cls.__name__ + ":" + cls.marker
                return inner()

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dunder_class_cell_escaping_nested_method",
        """
        class Base:
            marker = "base"

        class Child(Base):
            marker = "child"

            def make(self):
                def inner():
                    cls = __class__
                    return cls.__name__ + ":" + cls.marker
                return inner

        def main() -> None:
            fn = Child().make()
            print(fn())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dunder_class_local_shadow_escaping_nested_method",
        """
        class Base:
            marker = "base"

        class Child(Base):
            marker = "child"

            def make(self):
                __class__ = "local"
                def inner():
                    return "value:" + __class__
                return inner

        def main() -> None:
            fn = Child().make()
            print(fn())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dunder_class_cell_staticmethod",
        """
        class Base:
            marker = "base"

        class Child(Base):
            marker = "child"

            @staticmethod
            def label():
                cls = __class__
                return cls.__name__ + ":" + cls.marker

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "zero_arg_super_staticmethod_error",
        """
        class Base:
            @staticmethod
            def label():
                return "base"

        class Child(Base):
            @staticmethod
            def label():
                try:
                    return super().label()
                except RuntimeError:
                    return "runtime-error"

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_body_dunder_class_nameerror",
        """
        RESULT = "unset"

        try:
            class Host:
                value = __class__
            RESULT = "no-error"
        except NameError:
            RESULT = "name-error"

        def main() -> None:
            print(RESULT)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_method",
        """
        class Base:
            def label(self):
                return "base"

        class Child(Base):
            def label(self):
                return super(Child, self).label() + ":child"

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_classmethod",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is Child:
                    return "child"
                return "wrong"

        class Child(Base):
            @classmethod
            def label(cls):
                return super(Child, cls).label() + ":via-child"

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_invalid_receiver_typeerror",
        """
        class Base:
            def label(self):
                return "base"

        class Child(Base):
            def label(self):
                try:
                    return super(Child, Base()).label()
                except TypeError:
                    return "type-error"

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_staticmethod_explicit_receiver",
        """
        class Base:
            def label(self):
                return "base"

        class Child(Base):
            @staticmethod
            def label(obj):
                return super(Child, obj).label() + ":child"

        def main() -> None:
            print(Child.label(Child()))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_class_receiver_subtype",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is GrandChild:
                    return "grand"
                if cls is Child:
                    return "child"
                return "wrong"

        class Child(Base):
            @staticmethod
            def label():
                return super(Child, GrandChild).label()

        class GrandChild(Child):
            pass

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_class_alias_receiver_subtype",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is GrandChild:
                    return "grand"
                if cls is Child:
                    return "child"
                return "wrong"

        class Child(Base):
            @staticmethod
            def label():
                return super(AliasChild, AliasGrand).label()

        class GrandChild(Child):
            pass

        AliasChild = Child
        AliasGrand = GrandChild

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_local_class_alias_receiver_subtype",
        """
        class Base:
            @classmethod
            def label(cls):
                if cls is GrandChild:
                    return "grand"
                if cls is Child:
                    return "child"
                return "wrong"

        class Child(Base):
            @staticmethod
            def label():
                AliasChild = Child
                AliasGrand = GrandChild
                return super(AliasChild, AliasGrand).label()

        class GrandChild(Child):
            pass

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "argumented_super_missing_method_attributeerror",
        """
        class Base:
            def label(self):
                return "base"

        class Child(Base):
            def label(self):
                try:
                    return super(Child, self).missing()
                except AttributeError:
                    return "attribute-error"

        def main() -> None:
            print(Child().label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "inherited_classmethod_cls_class_attr",
        """
        class Base:
            @classmethod
            def label(cls):
                return cls.name + ":base"

        class Child(Base):
            name = "child"

        def main() -> None:
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_mutation_visible_to_classmethod",
        """
        class Base:
            @classmethod
            def label(cls):
                return cls.name + ":base"

        class Child(Base):
            name = "child"

        def main() -> None:
            print(Child.label())
            Child.name = "updated"
            print(Child.label())
            print(Child.name)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "inherited_classmethod_cls_attr_store",
        """
        class Base:
            name = "base"

            @classmethod
            def set_name(cls, value):
                cls.name = value

            @classmethod
            def label(cls):
                return cls.name + ":base"

        class Child(Base):
            name = "child"

        def main() -> None:
            print(Child.label())
            Child.set_name("updated")
            print(Child.label())
            print(Base.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_delete_visible_to_classmethod",
        """
        class Base:
            name = "base"

            @classmethod
            def label(cls):
                return cls.name + ":base"

        class Child(Base):
            name = "child"

        def main() -> None:
            print(Child.label())
            del Child.name
            print(Child.label())
            print(Child.name)
            print(Base.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_descriptor_get_owner",
        """
        class Descriptor:
            def __get__(self, obj, owner):
                if obj is None and owner is Child:
                    return "child-owner"
                if obj is None and owner is Base:
                    return "base-owner"
                return "instance"

        class Base:
            desc = Descriptor()

            @classmethod
            def label(cls):
                return cls.desc + ":label"

        class Child(Base):
            pass

        def main() -> None:
            print(Base.desc)
            print(Child.desc)
            print(Child.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_staticmethod_classmethod_wrapper_access",
        """
        class Base:
            @staticmethod
            def marker(value):
                return "static:" + value

            @classmethod
            def label(cls):
                if cls is Child:
                    return "child"
                return "base"

        class Child(Base):
            pass

        def main() -> None:
            static_fn = Child.marker
            class_fn = Child.label
            print(static_fn("x"))
            print(class_fn())
            print(Base.marker("y"))
            print(Base.label())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_instance_method_unbound_value",
        """
        class Base:
            def label(self, suffix):
                return self.name + suffix

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            fn = Child.label
            print(fn(Child(), ":value"))
            print(Base.label(Child(), ":base"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "instance_method_bound_name_self",
        """
        class Child:
            def __init__(self) -> None:
                self.name = "child"

            def label(self, suffix):
                return self.name + suffix

        def main() -> None:
            obj = Child()
            m1 = obj.label
            m2 = obj.label
            print(m1 is m2)
            print(m1.__name__)
            print(m1.__self__ is obj)
            print(m1.__self__.name)
            print(m1(":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dynamic_class_attr_function_instance_bound",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Child:
            def __init__(self) -> None:
                self.name = "child"

            def label(self, suffix):
                return self.name + suffix + ":old"

        def main() -> None:
            obj = Child()
            Child.label = replacement
            m1 = obj.label
            m2 = obj.label
            print(m1 is m2)
            print(m1.__name__)
            print(m1.__self__ is obj)
            print(m1.__self__.name)
            print(m1(":value"))
            print(Child.label.__name__)
            print(Child.label(obj, ":class"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_runtime_lookup",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":old"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            print(Child.label(Child(), ":before"))
            Child.label = replacement
            fn = Child.label
            print(fn(Child(), ":after"))
            print(Child.label(Child(), ":direct"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_delete_fallback",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            print(Child.label(Child(), ":set"))
            del Child.label
            fn = Child.label
            print(fn(Child(), ":value"))
            print(Child.label(Child(), ":direct"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_untaken_branch_fallback",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        def choose():
            return False

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            if choose():
                Child.label = replacement
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_taken_branch_lookup",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        def choose():
            return True

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            if choose():
                Child.label = replacement
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_loop_untaken_delete_preserves_replacement",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        def choose():
            return False

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            while choose():
                del Child.label
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_loop_taken_delete_fallback",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            count = 0
            while count == 0:
                del Child.label
                count = 1
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_try_except_untaken_delete_preserves_replacement",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            try:
                value = "safe"
            except ValueError:
                del Child.label
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_try_except_taken_delete_fallback",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            try:
                raise ValueError("boom")
            except ValueError:
                del Child.label
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_finally_delete_fallback",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            try:
                value = "safe"
            finally:
                del Child.label
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_finally_store_after_delete",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            try:
                del Child.label
            finally:
                Child.label = replacement
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_loop_break_delete_fallback",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            while True:
                del Child.label
                break
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_method_replacement_loop_continue_skips_delete",
        """
        def replacement(self, suffix):
            return self.name + suffix + ":new"

        class Base:
            def label(self, suffix):
                return self.name + suffix + ":base"

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            Child.label = replacement
            count = 0
            while count == 0:
                count = 1
                continue
                del Child.label
            print(Child.label(Child(), ":direct"))
            fn = Child.label
            print(fn(Child(), ":value"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_descriptor_replacement_runtime_lookup",
        """
        class Descriptor:
            def __init__(self, tag):
                self.tag = tag

            def __get__(self, obj, owner):
                if obj is None and owner is Child:
                    return self.tag + ":child"
                if obj is None and owner is Base:
                    return self.tag + ":base"
                return self.tag + ":instance"

        class Base:
            desc = Descriptor("base")

            @classmethod
            def label(cls):
                return cls.desc + ":label"

        class Child(Base):
            pass

        def main() -> None:
            print(Child.desc)
            Child.desc = Descriptor("new")
            print(Child.desc)
            print(Child.label())
            print(Base.desc)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_descriptor_replacement_delete_fallback",
        """
        class Descriptor:
            def __init__(self, tag):
                self.tag = tag

            def __get__(self, obj, owner):
                if obj is None and owner is Child:
                    return self.tag + ":child"
                if obj is None and owner is Base:
                    return self.tag + ":base"
                return self.tag + ":instance"

        class Base:
            desc = Descriptor("base")

            @classmethod
            def label(cls):
                return cls.desc + ":label"

        class Child(Base):
            pass

        def main() -> None:
            Child.desc = Descriptor("new")
            print(Child.desc)
            del Child.desc
            print(Child.desc)
            print(Child.label())
            print(Base.desc)

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_function_descriptor_identity",
        """
        class Base:
            def label(self, suffix):
                return self.name + suffix

            @staticmethod
            def marker(value):
                return "static:" + value

        class Child(Base):
            def __init__(self) -> None:
                self.name = "child"

        def main() -> None:
            fn1 = Child.label
            fn2 = Child.label
            print(fn1 is fn2)
            print(fn1.__name__)
            print(fn1(Child(), ":value"))
            static1 = Child.marker
            static2 = Child.marker
            print(static1 is static2)
            print(static1.__name__)
            print(static1("x"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_classmethod_bound_name_identity",
        """
        class Base:
            name = "base"

            @classmethod
            def label(cls, suffix):
                return cls.name + suffix

        class Child(Base):
            name = "child"

        def main() -> None:
            cm1 = Child.label
            cm2 = Child.label
            print(cm1 is cm2)
            print(cm1.__name__)
            print(cm1(":value"))
            print(Base.label.__name__)
            print(Base.label(":base"))

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_attr_classmethod_bound_self",
        """
        class Base:
            name = "base"

            @classmethod
            def label(cls):
                return cls.name

        class Child(Base):
            name = "child"

        def main() -> None:
            cm = Child.label
            print(cm.__self__ is Child)
            print(cm.__self__.name)
            print(cm())
            base = Base.label
            print(base.__self__ is Base)
            print(base.__self__.name)
            print(base())

        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "try_except",
        """
        def f(n):
            try:
                if n == 0:
                    raise ValueError("zero")
                return n
            except ValueError:
                return 42

        def main() -> None:
            print(f(0))
            print(f(5))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "varargs",
        """
        def pick(*args):
            print(args[0])
            print(args[2])

        def main() -> None:
            pick(4, 5, 6)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "dict_ops",
        """
        def main() -> None:
            d = {"a": 1, "b": 2}
            d["c"] = d["a"] + d["b"]
            print(d["c"])
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "string_methods",
        """
        def main() -> None:
            s = "  abc  "
            print(s.strip())
            print("abc".startswith("a"))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "lambda_add",
        """
        def main() -> None:
            f = lambda x, y: x + y
            print(f(3, 4))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "kwargs_defaults",
        """
        def f(a, b=20, c=100):
            return a + b + c

        def main() -> None:
            print(f(1, 10, 20))
            print(f(1, c=2))
            print(f(1, b=9))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "list_literal",
        """
        def main() -> None:
            xs = [1, 2, 3]
            print(xs[1])
            print(len(xs))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "while_break_continue",
        """
        def main() -> None:
            i = 0
            total = 0
            while i < 6:
                i = i + 1
                if i == 2:
                    continue
                if i == 5:
                    break
                total = total + i
            print(total)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "for_range_sum",
        """
        def main() -> None:
            total = 0
            for i in range(5):
                total = total + i
            print(total)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "bool_short_circuit",
        """
        def side():
            print("side")
            return True

        def main() -> None:
            print(False and side())
            print(True or side())
            print(True and side())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "short_circuit_value_semantics",
        """
        def side(label: str):
            print(label)
            return label

        def main() -> None:
            print("left" or side("bad_or"))
            print("" or side("right_or"))
            print("" and side("bad_and"))
            print("left" and side("right_and"))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "short_circuit_custom_bool",
        """
        class Flag:
            def __init__(self, name: str, truth: bool) -> None:
                self.name = name
                self.truth = truth

            def __bool__(self) -> bool:
                print("bool:" + self.name)
                return self.truth

            def __str__(self) -> str:
                return self.name

        def main() -> None:
            yes = Flag("yes", True)
            no = Flag("no", False)
            print(yes or "bad")
            print(no or "fallback")
            print(no and "bad")
            print(yes and "rhs")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "short_circuit_bool_exception",
        """
        class Boom:
            def __bool__(self) -> bool:
                print("bool:boom")
                raise ValueError("boom")

        def main() -> None:
            try:
                print(Boom() or "fallback")
            except ValueError:
                print("caught_or")
            try:
                print(Boom() and "rhs")
            except ValueError:
                print("caught_and")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "short_circuit_len_truthiness",
        """
        class Bag:
            def __init__(self, name: str, size: int) -> None:
                self.name = name
                self.size = size

            def __len__(self) -> int:
                print("len:" + self.name)
                return self.size

            def __str__(self) -> str:
                return self.name

        def main() -> None:
            full = Bag("full", 2)
            empty = Bag("empty", 0)
            print(full or "bad")
            print(empty or "fallback")
            print(empty and "bad")
            print(full and "rhs")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "short_circuit_len_exception",
        """
        class BadLen:
            def __len__(self) -> int:
                print("len:bad")
                raise ValueError("bad_len")

        def main() -> None:
            try:
                print(BadLen() or "fallback")
            except ValueError:
                print("caught_or")
            try:
                print(BadLen() and "rhs")
            except ValueError:
                print("caught_and")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "short_circuit_len_negative",
        """
        class NegativeLen:
            def __len__(self) -> int:
                print("len:neg")
                return -1

        def main() -> None:
            try:
                print(NegativeLen() or "fallback")
            except ValueError:
                print("caught_or")
            try:
                print(NegativeLen() and "rhs")
            except ValueError:
                print("caught_and")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "getattr_default_and_if_args_or_kwargs",
        """
        class Node:
            def __init__(self) -> None:
                self.tag = "value"

        def call_ident(expr):
            return getattr(expr, "tag", "missing")

        def classify(args, kwargs):
            if args or kwargs:
                print("nonempty")
            else:
                print("empty")

        def main() -> None:
            print(call_ident(Node()))
            print(call_ident(object()))

            classify([], {})
            classify(["x"], {})
            classify([], {"k": "v"})
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "getattr_default_missing_attr",
        """
        class Node:
            def __init__(self) -> None:
                self.tag = "present"

        def main() -> None:
            n = Node()
            m = object()
            print(getattr(n, "tag", None))
            print(getattr(m, "tag", "missing"))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "is_none",
        """
        def main() -> None:
            x = None
            print(x is None)
            print(x is not None)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "tuple_unpack_index",
        """
        def main() -> None:
            a, b = (4, 5)
            t = (1, "x", 3)
            print(a + b)
            print(t[2])
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "list_append_pop",
        """
        def main() -> None:
            xs = [1]
            xs.append(2)
            xs.append(3)
            print(xs.pop())
            print(len(xs))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "list_assignment",
        """
        def main() -> None:
            xs = [1, 2, 3]
            xs[1] = 9
            print(xs[0] + xs[1] + xs[2])
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "string_split_join",
        """
        def main() -> None:
            xs = "a,b,c".split(",")
            print(xs[1])
            print("-".join(xs))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "string_replace_find",
        """
        def main() -> None:
            s = "abcabc"
            print(s.find("ca"))
            print(s.replace("ab", "x"))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "int_ops",
        """
        def main() -> None:
            print(7 // 3)
            print(7 % 3)
            print((1 << 5) + (16 >> 2))
            print(~1)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "float_arith",
        """
        def main() -> None:
            print(1.5 + 2.25)
            print(5.0 / 2.0)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "typed_args_return",
        """
        def mul(a: int, b: int) -> int:
            return a * b

        def main() -> None:
            print(mul(6, 7))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "keyword_only",
        """
        def f(a, *, b=4):
            return a + b

        def main() -> None:
            print(f(3, b=5))
            print(f(3))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "nested_def_no_capture",
        """
        def outer():
            def inner(x):
                return x + 1
            return inner(4)

        def main() -> None:
            print(outer())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "nested_def_capture",
        """
        def outer(n):
            def inner(x):
                return x + n
            return inner(4)

        def main() -> None:
            print(outer(3))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "closure_cell_rebind",
        """
        def outer():
            x = 1
            def get():
                return x
            f = get
            x = 2
            return f

        def main() -> None:
            print(outer()())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "list_slice_mutation",
        """
        def main() -> None:
            xs = [1, 2, 3, 4]
            xs[1:3] = [8, 9, 10]
            print(len(xs))
            print(xs[1])
            print(xs[3])
            del xs[1:3]
            print(len(xs))
            print(xs[1])
            xs[2:1] = [11]
            print(len(xs))
            print(xs[2])
            print(xs[3])

            ys = [0, 1, 2, 3, 4, 5]
            ys[1:6:2] = [7, 8, 9]
            print(ys[1])
            print(ys[3])
            print(ys[5])
            del ys[::2]
            print(len(ys))
            print(ys[0])
            print(ys[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "recursion_factorial",
        """
        def fact(n):
            if n <= 1:
                return 1
            return n * fact(n - 1)

        def main() -> None:
            print(fact(5))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "try_finally",
        """
        def main() -> None:
            try:
                print("body")
            finally:
                print("finally")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "raise_except_as",
        """
        def main() -> None:
            try:
                raise ValueError("bad")
            except ValueError as e:
                print("caught")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "class_inheritance",
        """
        class A:
            def f(self):
                return 1
        class B(A):
            def g(self):
                return self.f() + 2

        def main() -> None:
            b = B()
            print(b.g())
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "list_comprehension",
        """
        def main() -> None:
            xs = [1, 2, 3, 4]
            ys = [x * 2 for x in xs if x > 2]
            print(ys[0])
            print(len(ys))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "enumerate_loop",
        """
        def main() -> None:
            xs = [3, 4, 5]
            total = 0
            for i, x in enumerate(xs):
                total = total + i * x
            print(total)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "isinstance_int",
        """
        def f(x):
            if isinstance(x, int):
                return x + 2
            return 0

        def main() -> None:
            print(f(3))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "slicing",
        """
        def main() -> None:
            s = "abcdef"
            xs = [1, 2, 3, 4]
            print(s[1:4])
            print(xs[1:3][0])
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "unary_compare_mix",
        """
        def main() -> None:
            x = -5
            print(+x)
            print(not (x > 0))
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "delete_name",
        """
        def main() -> None:
            x = 3
            print(x)
            del x
            y = 4
            print(y)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "default_str_arg",
        """
        def f(prefix="x", value=3):
            print(prefix)
            print(value)

        def main() -> None:
            f(value=7)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "tuple_return",
        """
        def pair(x):
            return (x, x + 1)

        def main() -> None:
            a = pair(8)
            print(a[0])
            print(a[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "generator_next",
        """
        def counter():
            i = 0
            while i < 3:
                yield i
                i = i + 1

        def main() -> None:
            g = counter()
            print(next(g))
            print(next(g))
            print(next(g))
            try:
                next(g)
                print("no_stop")
            except StopIteration:
                print("StopIteration")
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "generator_yield_from",
        """
        def inner():
            yield 1
            yield 2

        def outer():
            yield 0
            yield from inner()
            yield 3

        def main() -> None:
            for value in outer():
                print(value)
        if __name__ == "__main__":
            main()
        """,
    ),
    (
        "generator_inner_for",
        """
        def gen():
            for value in [1, 2, 3]:
                yield value

        def main() -> None:
            for value in gen():
                print(value)
        if __name__ == "__main__":
            main()
        """,
    ),
)

PARITY_MANIFEST_PATH = REPO_ROOT / "tests" / "host_pcc_pcc1_parity_manifest.json"
PARITY_APPLICABILITY = load_applicability_manifest(PARITY_MANIFEST_PATH, CASES)
APPLICABLE_PARITY_CASES = PARITY_APPLICABILITY.applicable
CORPUS_PARITY_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "host_pcc_pcc1_corpus_parity_manifest.json"
)
CORPUS_PARITY_INPUTS = tuple(
    (case.name, case.source) for case in PYTHON_CORPUS_CASES
)
CORPUS_PARITY_APPLICABILITY = load_applicability_manifest(
    CORPUS_PARITY_MANIFEST_PATH,
    CORPUS_PARITY_INPUTS,
    registry_id="tests.py_corpus_support.PYTHON_CORPUS_CASES",
)
APPLICABLE_CORPUS_PARITY_CASES = CORPUS_PARITY_APPLICABILITY.applicable
CORPUS_CASE_BY_NAME = {case.name: case for case in PYTHON_CORPUS_CASES}


def _intent_parity_inputs() -> tuple[tuple[str, str], ...]:
    """Expose every source-program intent oracle to the pcc1 parity gate.

    Prefixes preserve the originating contract, so equal short ids in two
    registries cannot silently collapse.  GC programs run here in the default
    backend; their five-backend equality remains a separate final gate.
    """

    inputs: list[tuple[str, str]] = []
    groups = (
        ("semantics", intent_constraints.SEMANTICS_CASES),
        ("error", intent_constraints.ERROR_MODEL_CASES),
        ("stdlib", intent_constraints.STDLIB_CASES),
        ("identity", intent_constraints.IDENTITY_CASES),
        ("bignum", intent_constraints.INT_BIGNUM_CASES),
        ("gc_default", intent_constraints.GC_PROGRAMS),
    )
    for prefix, cases in groups:
        for name, source in cases:
            inputs.append((f"{prefix}__{name}", source))
    for name, source, _expected in intent_constraints.VALUECLASS_CASES:
        inputs.append((f"valueclass__{name}", source))
    for seed in intent_constraints.METAMORPHIC_SEEDS:
        inputs.append(
            (f"metamorphic_int__{seed}", intent_constraints._gen_program(seed))
        )
    for seed in intent_constraints.STR_LIST_SEEDS:
        inputs.append(
            (
                f"metamorphic_str_list__{seed}",
                intent_constraints._gen_str_list_program(seed),
            )
        )
    inputs.extend(
        (
            (
                "valueclass__method_field_access",
                intent_constraints.VALUECLASS_GAP_SRC,
            ),
            (
                "valueclass__genexpr_field_access",
                intent_constraints.VALUECLASS_GENEXPR_GAP_SRC,
            ),
        )
    )
    return tuple(inputs)


INTENT_PARITY_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "host_pcc_pcc1_intent_parity_manifest.json"
)
INTENT_PARITY_INPUTS = _intent_parity_inputs()
INTENT_PARITY_APPLICABILITY = load_applicability_manifest(
    INTENT_PARITY_MANIFEST_PATH,
    INTENT_PARITY_INPUTS,
    registry_id="tests.python.test_intent_constraints.HOST_PCC_PROGRAMS",
)
APPLICABLE_INTENT_PARITY_CASES = INTENT_PARITY_APPLICABILITY.applicable

FIXPOINT_SMOKE_CASES = frozenset(
    {
        "ternary_inline",
        "lambda_add",
        "kwargs_defaults",
        "list_comprehension",
        "nested_def_capture",
        "closure_cell_rebind",
        "fstring_format_spec",
        "bitwise_int_ops",
        "bitwise_negative_shift_errors",
        "fstring_debug_and_conversions",
        "fstring_attr_call_mix",
        "fstring_dynamic_format_spec",
        "fstring_custom_format",
        "string_concat_runtime",
        "fstring_ascii_conversion_non_ascii",
        "generator_fstring_join",
        "tuple_generator_expression",
        "walrus_expression",
        "list_slice_mutation",
        "generator_next",
        "getattr_default_and_if_args_or_kwargs",
        "generator_yield_from",
        "generator_inner_for",
        "custom_getattribute",
        "custom_getattribute_getattr_fallback",
        "custom_getattribute_valueerror_propagates",
        "metaclass_literal_class_attrs",
        "metaclass_method_binding",
        "metaclass_property_binding",
        "metaclass_property_readonly_errors",
        "metaclass_property_readonly_precedence",
        "metaclass_custom_descriptor_set_delete",
        "metaclass_data_descriptor_precedence",
        "metaclass_runtime_class_object_property_precedence",
        "metaclass_runtime_class_object_data_descriptor_precedence",
        "metaclass_runtime_class_object_property_setter_deleter_precedence",
        "metaclass_prepare_namespace",
        "metaclass_prepare_namespace_body_override",
        "metaclass_prepare_namespace_body_extends",
        "metaclass_prepare_namespace_method_and_body",
        "metaclass_prepare_namespace_non_mapping_typeerror",
        "metaclass_prepare_custom_namespace_setitem_order",
        "metaclass_prepare_custom_namespace_getitem_new",
        "metaclass_prepare_custom_namespace_class_body_lookup",
        "metaclass_prepare_custom_namespace_constructor_args",
        "metaclass_prepare_custom_namespace_factory_return",
        "metaclass_prepare_custom_namespace_alias_constructor",
        "metaclass_prepare_custom_namespace_factory_local_return",
        "metaclass_prepare_custom_namespace_generic_mapping_factory",
        "metaclass_prepare_custom_namespace_delete_name",
        "metaclass_keyword_arguments_prepare_new",
        "metaclass_call_controls_instantiation",
        "metaclass_call_delegates_type_call",
        "metaclass_dynamic_value_binding",
        "metaclass_dynamic_value_exception_propagates",
        "metaclass_dynamic_value_function_return",
        "metaclass_dynamic_value_function_arg_return",
        "metaclass_dynamic_value_conditional_return",
        "metaclass_dynamic_value_conditional_expr",
        "metaclass_dynamic_value_bool_or_expr",
        "metaclass_dynamic_value_bool_and_expr",
        "metaclass_dynamic_value_bool_or_falsey_left_expr",
        "metaclass_dynamic_value_bool_or_alias_fallback",
        "metaclass_dynamic_value_bool_and_or_falsey_chain",
        "metaclass_prepare_namespace_non_string_key_typeerror",
        "metaclass_inherited_from_base",
        "metaclass_conflict_between_bases_typeerror",
        "metaclass_compatible_bases_choose_most_derived",
        "dynamic_type_constructor_class_attrs",
        "dynamic_type_constructor_namespace_var",
        "dynamic_type_constructor_namespace_alias",
        "dynamic_type_constructor_namespace_mutation_before_call",
        "dynamic_type_constructor_runtime_namespace_dict",
        "dynamic_type_constructor_namespace_mapping_typeerror",
        "dynamic_type_constructor_namespace_function_method",
        "dynamic_type_constructor_namespace_descriptor",
        "dynamic_type_constructor_namespace_staticmethod",
        "dynamic_type_constructor_namespace_classmethod",
        "dynamic_type_constructor_namespace_data_descriptor",
        "dynamic_type_constructor_namespace_property",
        "dynamic_type_constructor_namespace_property_readonly_errors",
        "property_decorator_get_set_delete",
        "descriptor_get",
        "data_descriptor_priority",
        "user_instance_subscript_setitem_getitem",
        "descriptor_delete",
        "zero_arg_super_method",
        "zero_arg_super_classmethod",
        "zero_arg_super_nested_method_with_receiver",
        "zero_arg_super_nested_class_receiver",
        "zero_arg_super_escaping_nested_class_receiver",
        "zero_arg_super_escaping_nested_method_receiver",
        "dunder_class_cell_method",
        "dunder_class_cell_nested_method",
        "dunder_class_cell_escaping_nested_method",
        "dunder_class_local_shadow_escaping_nested_method",
        "dunder_class_cell_staticmethod",
        "zero_arg_super_staticmethod_error",
        "class_body_dunder_class_nameerror",
        "argumented_super_method",
        "argumented_super_classmethod",
        "argumented_super_invalid_receiver_typeerror",
        "argumented_super_staticmethod_explicit_receiver",
        "argumented_super_class_receiver_subtype",
        "argumented_super_class_alias_receiver_subtype",
        "argumented_super_local_class_alias_receiver_subtype",
        "argumented_super_missing_method_attributeerror",
        "inherited_classmethod_cls_class_attr",
        "class_attr_mutation_visible_to_classmethod",
        "inherited_classmethod_cls_attr_store",
        "class_attr_delete_visible_to_classmethod",
        "class_attr_descriptor_get_owner",
        "class_attr_staticmethod_classmethod_wrapper_access",
        "class_attr_instance_method_unbound_value",
        "instance_method_bound_name_self",
        "dynamic_class_attr_function_instance_bound",
        "class_attr_method_replacement_runtime_lookup",
        "class_attr_method_replacement_delete_fallback",
        "class_attr_method_replacement_untaken_branch_fallback",
        "class_attr_method_replacement_taken_branch_lookup",
        "class_attr_method_replacement_loop_untaken_delete_preserves_replacement",
        "class_attr_method_replacement_loop_taken_delete_fallback",
        "class_attr_method_replacement_try_except_untaken_delete_preserves_replacement",
        "class_attr_method_replacement_try_except_taken_delete_fallback",
        "class_attr_method_replacement_finally_delete_fallback",
        "class_attr_method_replacement_finally_store_after_delete",
        "class_attr_method_replacement_loop_break_delete_fallback",
        "class_attr_method_replacement_loop_continue_skips_delete",
        "class_attr_descriptor_replacement_runtime_lookup",
        "class_attr_descriptor_replacement_delete_fallback",
        "class_attr_function_descriptor_identity",
        "class_attr_classmethod_bound_name_identity",
        "class_attr_classmethod_bound_self",
        "try_except",
        "bool_short_circuit",
        "short_circuit_value_semantics",
        "short_circuit_custom_bool",
        "short_circuit_bool_exception",
        "short_circuit_len_truthiness",
        "short_circuit_len_exception",
        "short_circuit_len_negative",
        "float_arith",
    }
)

FIXPOINT_CASES = tuple(
    (name, source) for name, source in CASES if name in FIXPOINT_SMOKE_CASES
)

# Set from RECORDED current-source stage costs, not widened as a reflex
# (INFRA-P1-SELF-HOST-ORACLE-WARMUP-BUDGET).  The oracle builds stages with
# 4 frontend jobs; the recorded strict-cold stage2 at that shape was
# 1290.9 s (2026-08-26 evidence), and the 2026-08-27 admission-wave landing
# takes ~58 s off the oversized lane
# (docs/goal/evidence/2026-08-27-oversized-admission-waves-implemented.md:
# 10-job cold stage2 664.2 s quiet / 867.8 s loaded).  1500 covers the
# recorded 4-job cold cost with margin while still going red on a real
# regression; PERF-P0-STAGE2-COLD-CACHE-REGRESSION owns shrinking the cost
# itself.
_SELF_HOST_BUILD_TIMEOUT_SECONDS = 1500
_SELF_HOST_STAGE_FRONTEND_JOBS = "4"


def _stage_budget_timeout_message(stage_label: str) -> str:
    return (
        f"self-host oracle {stage_label} exceeded its "
        f"{_SELF_HOST_BUILD_TIMEOUT_SECONDS}s budget. This budget is set "
        "from the RECORDED cold stage costs (4-job strict-cold stage2 "
        "1290.9s on 2026-08-26; 10-job cold 664.2s quiet / 867.8s loaded "
        "on 2026-08-27), so exceeding it means a stage-cost regression "
        "beyond the recorded envelope or a severely loaded machine — "
        "re-measure the stage cost before widening the budget "
        "(INFRA-P1-SELF-HOST-ORACLE-WARMUP-BUDGET)."
    )


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    # A stage build is serialized by _self_host_artifact_lock, so it owns a
    # bounded inner budget rather than inheriting xdist's outer-width divisor.
    # Two workers miss the cold 600-second stage deadline on the reference Mac;
    # four remain below the repository memory ceiling and finish in time.
    env.setdefault("PCC_PY_FRONTEND_JOBS", _SELF_HOST_STAGE_FRONTEND_JOBS)
    env.setdefault("PCC_SELF_BACKEND_OBJECT_CACHE", "1")
    env.setdefault(
        "PCC_SELF_BACKEND_OBJECT_CACHE_DIR",
        str(self_host_object_cache_dir()),
    )
    env["PCC_PY_FRONTEND_IR_CACHE_IDENTITY"] = self_host_source_key()
    env["PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY"] = self_backend_object_cache_key()
    return env


def _supported_self_host() -> bool:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return machine in {"arm64", "aarch64"}
    if sys.platform.startswith("linux"):
        return machine in {"x86_64", "amd64"}
    return False


def _shared_self_host_oracle_dir(tmp_path_factory, worker_id: str) -> Path:
    """Return one source-addressed artifact directory for every worker/run."""

    del tmp_path_factory, worker_id
    return cached_self_host_oracle_dir()


@contextmanager
def _self_host_artifact_lock(path: Path):
    import fcntl

    stream = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _selected_pcc1_receipt_path(pcc1: Path) -> Path:
    if os.environ.get("PCC1_BINARY"):
        return Path(
            os.environ.get("PCC1_RECEIPT", str(pcc1_receipt_path(pcc1)))
        ).resolve()
    return pcc1_receipt_path(pcc1)


@pytest.fixture(scope="session")
def pcc1_self_host_binary(tmp_path_factory, worker_id):
    if not _supported_self_host():
        pytest.fail("self backend oracle supports macOS arm64 and Linux x86_64")
    explicit_pcc1 = os.environ.get("PCC1_BINARY")
    if explicit_pcc1:
        pcc1 = Path(explicit_pcc1).resolve()
        assert pcc1.is_file(), f"PCC1_BINARY does not exist: {pcc1}"
        receipt = _selected_pcc1_receipt_path(pcc1)
        try:
            verify_pcc1_receipt(
                pcc1,
                receipt,
                source_key=self_host_source_key(),
                object_cache_identity=self_backend_object_cache_key(),
            )
        except ParityContractError as exc:
            pytest.fail(f"PCC1_BINARY is not receipt-verified: {exc}")
        return pcc1
    out_dir = _shared_self_host_oracle_dir(tmp_path_factory, worker_id)
    pcc1 = out_dir / "pcc1"
    receipt = pcc1_receipt_path(pcc1)
    source_key = self_host_source_key()
    object_cache_identity = self_backend_object_cache_key()
    with _self_host_artifact_lock(out_dir / "pcc1.lock"):
        if pcc1.is_file():
            try:
                verify_pcc1_receipt(
                    pcc1,
                    receipt,
                    source_key=source_key,
                    object_cache_identity=object_cache_identity,
                )
            except ParityContractError:
                pass
            else:
                return pcc1
        temporary = out_dir / f".pcc1.{os.getpid()}.tmp"
        try:
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pcc",
                        "--python-libpython",
                        "off",
                        "--backend",
                        "self",
                        "--ir-scaffold",
                        "on",
                        "pcc/__main__.py",
                        "-o",
                        str(temporary),
                    ],
                    cwd=str(REPO_ROOT),
                    env=_child_env(),
                    capture_output=True,
                    text=True,
                    timeout=_SELF_HOST_BUILD_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(
                    _stage_budget_timeout_message("stage1 (pcc0 -> pcc1)")
                ) from exc
            assert result.returncode == 0, (
                "failed to build pcc1 for self-host oracle\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
            os.replace(temporary, pcc1)
            write_pcc1_receipt(
                pcc1,
                receipt,
                source_key=source_key,
                object_cache_identity=object_cache_identity,
            )
        finally:
            if temporary.exists():
                temporary.unlink()
    verify_pcc1_receipt(
        pcc1,
        receipt,
        source_key=source_key,
        object_cache_identity=object_cache_identity,
    )
    return pcc1


def _build_next_stage(
    compiler: Path,
    out: Path,
) -> Path:
    try:
        result = subprocess.run(
            [
                str(compiler),
                "--python-libpython",
                "off",
                "--backend",
                "self",
                "pcc/__main__.py",
                "-o",
                str(out),
            ],
            cwd=str(REPO_ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=_SELF_HOST_BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            _stage_budget_timeout_message(
                f"stage build ({compiler.name} -> {out.name})"
            )
        ) from exc
    assert result.returncode == 0, (
        f"failed to build {out.name} for self-host oracle\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return out


@pytest.fixture(scope="session")
def pcc2_self_host_binary(tmp_path_factory, worker_id, pcc1_self_host_binary):
    explicit_pcc2 = os.environ.get("PCC2_BINARY")
    if explicit_pcc2:
        pcc2 = Path(explicit_pcc2).resolve()
        assert pcc2.is_file(), f"PCC2_BINARY does not exist: {pcc2}"
        return pcc2
    out_dir = _shared_self_host_oracle_dir(tmp_path_factory, worker_id)
    pcc2 = out_dir / "pcc2"
    with _self_host_artifact_lock(out_dir / "pcc2.lock"):
        if not pcc2.is_file():
            temporary = out_dir / f".pcc2.{os.getpid()}.tmp"
            try:
                _build_next_stage(pcc1_self_host_binary, temporary)
                os.replace(temporary, pcc2)
            finally:
                if temporary.exists():
                    temporary.unlink()
    return pcc2


@pytest.fixture(scope="session")
def pcc3_self_host_binary(tmp_path_factory, worker_id, pcc2_self_host_binary):
    explicit_pcc3 = os.environ.get("PCC3_BINARY")
    if explicit_pcc3:
        pcc3 = Path(explicit_pcc3).resolve()
        assert pcc3.is_file(), f"PCC3_BINARY does not exist: {pcc3}"
        return pcc3
    out_dir = _shared_self_host_oracle_dir(tmp_path_factory, worker_id)
    pcc3 = out_dir / "pcc3"
    with _self_host_artifact_lock(out_dir / "pcc3.lock"):
        if not pcc3.is_file():
            temporary = out_dir / f".pcc3.{os.getpid()}.tmp"
            try:
                _build_next_stage(pcc2_self_host_binary, temporary)
                os.replace(temporary, pcc3)
            finally:
                if temporary.exists():
                    temporary.unlink()
    return pcc3


def test_000_self_host_oracle_stage_cache_warmup(
    pcc1_self_host_binary,
    pcc2_self_host_binary,
    pcc3_self_host_binary,
):
    """Start the unavoidable stage chain early so it overlaps other tests."""

    assert pcc1_self_host_binary.is_file()
    assert pcc2_self_host_binary.is_file()
    assert pcc3_self_host_binary.is_file()


def _links_libpython(binary: Path) -> bool:
    cmd = (
        ["otool", "-L", str(binary)]
        if sys.platform == "darwin"
        else [
            "ldd",
            str(binary),
        ]
    )
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout + result.stderr
    return "libpython" in text or "Python.framework" in text


def _signature_stripped_copy(src: Path, dst: Path) -> Path:
    shutil.copyfile(src, dst)
    if sys.platform == "darwin":
        subprocess.run(
            ["codesign", "--remove-signature", str(dst)],
            cwd=str(REPO_ROOT),
            env=_child_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        normalize_macho_metadata(dst)
    return dst


def _compile_and_run(
    compiler: list[str],
    src: Path,
    out: Path,
) -> subprocess.CompletedProcess[str]:
    compile_result = subprocess.run(
        compiler
        + [
            "--python-libpython",
            "off",
            "--backend",
            "self",
            str(src),
            "-o",
            str(out),
        ],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert compile_result.returncode == 0, (
        f"compile failed for {src.name}\n"
        f"stdout:\n{compile_result.stdout}\n"
        f"stderr:\n{compile_result.stderr}"
    )
    return subprocess.run(
        [str(out)],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _parity_subprocess_record(
    command: list[str],
    *,
    timeout: int,
) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": _subprocess_text(exc.stdout),
            "stderr": _subprocess_text(exc.stderr),
            "timed_out": True,
            "timeout_seconds": timeout,
        }
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": False,
        "timeout_seconds": timeout,
    }


def _compile_and_run_parity_observation(
    compiler: list[str],
    src: Path,
    out: Path,
) -> dict[str, object]:
    compile_command = compiler + [
        "--python-libpython",
        "off",
        "--backend",
        "self",
        "--ir-scaffold",
        "on",
        str(src),
        "-o",
        str(out),
    ]
    compile_record = _parity_subprocess_record(compile_command, timeout=90)
    observation: dict[str, object] = {
        "compile": compile_record,
        "run": None,
    }
    if compile_record["timed_out"] or compile_record["returncode"] != 0:
        return observation
    observation["run"] = _parity_subprocess_record([str(out)], timeout=20)
    return observation


def _parity_problem(
    host: dict[str, object],
    pcc1: dict[str, object] | None,
) -> str | None:
    host_compile = host["compile"]
    assert isinstance(host_compile, dict)
    if host_compile["timed_out"]:
        return "host pcc compile timed out"
    if host_compile["returncode"] != 0:
        return "host pcc compile failed"
    host_run = host["run"]
    if not isinstance(host_run, dict):
        return "host pcc compile succeeded without a runtime observation"
    if host_run["timed_out"]:
        return "host-pcc artifact timed out"
    if pcc1 is None:
        return "pcc1 observation was not produced after host pcc succeeded"
    pcc1_compile = pcc1["compile"]
    assert isinstance(pcc1_compile, dict)
    if pcc1_compile["timed_out"]:
        return "pcc1 compile timed out"
    if pcc1_compile["returncode"] != 0:
        return "pcc1 compile failed"
    pcc1_run = pcc1["run"]
    if not isinstance(pcc1_run, dict):
        return "pcc1 compile succeeded without a runtime observation"
    if pcc1_run["timed_out"]:
        return "pcc1 artifact timed out"
    for field in ("returncode", "stdout", "stderr"):
        if pcc1_run[field] != host_run[field]:
            return f"pcc1 runtime {field} differs from host pcc"
    return None


def _fail_host_pcc_pcc1_parity(
    *,
    request,
    name: str,
    src: Path,
    pcc1_binary: Path,
    host: dict[str, object],
    pcc1: dict[str, object] | None,
    problem: str,
    manifest_path: Path,
    candidate_names_digest: str,
    oracle: dict[str, object] | None = None,
) -> None:
    source_key = self_host_source_key()
    receipt = verify_pcc1_receipt(
        pcc1_binary,
        _selected_pcc1_receipt_path(pcc1_binary),
        source_key=source_key,
        object_cache_identity=self_backend_object_cache_key(),
    )
    report = write_parity_failure_report(
        source_key=source_key,
        case_name=name,
        payload={
            "problem": problem,
            "pytest_nodeid": request.node.nodeid,
            "stages": ["host-pcc-to-program", "pcc1-to-program"],
            "mode": {
                "backend": "self",
                "ir_scaffold": "on",
                "python_libpython": "off",
            },
            "manifest": str(manifest_path),
            "candidate_names_sha256": candidate_names_digest,
            "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
            "pcc1_binary": str(pcc1_binary),
            "pcc1_receipt": receipt,
            "oracle": oracle,
            "host_pcc": host,
            "pcc1": pcc1,
        },
    )
    pytest.fail(f"{problem}; durable failure report: {report}", pytrace=False)


def _corpus_oracle_problem(
    host: dict[str, object],
    case: PythonCorpusCase,
) -> str | None:
    run = host["run"]
    if not isinstance(run, dict):
        return "host pcc produced no corpus runtime observation"
    if run["returncode"] != case.expected_status:
        return "host pcc runtime status differs from checked-in corpus oracle"
    stdout = run["stdout"]
    if not isinstance(stdout, str) or stdout.encode("utf-8") != case.expected_stdout:
        return "host pcc stdout differs from checked-in corpus oracle"
    return None


def _run_python(src: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(src)],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _compile_and_run_artifact_no_host(
    compiler: list[str],
    src: Path,
    out: Path,
) -> subprocess.CompletedProcess[str]:
    # The self backend still uses a host subprocess to emit native code.
    # This gate proves the generated artifact's runtime behavior with host
    # Python disabled, not host-free native emission.
    compile_env = _child_env()

    compile_result = subprocess.run(
        compiler
        + [
            "--python-libpython",
            "off",
            "--backend",
            "self",
            str(src),
            "-o",
            str(out),
        ],
        cwd=str(REPO_ROOT),
        env=compile_env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert compile_result.returncode == 0, (
        f"compile failed for {src.name} before no-host artifact run\n"
        f"stdout:\n{compile_result.stdout}\n"
        f"stderr:\n{compile_result.stderr}"
    )

    run_env = _child_env()
    run_env["PCC_HOST_PYTHON"] = NO_HOST_PYTHON
    return subprocess.run(
        [str(out)],
        cwd=str(REPO_ROOT),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _duplicate_definition_symbols(ir_text: str) -> list[str]:
    symbols: list[str] = []
    for match in re.finditer(
        r'^define\b[^\n]*@(?:"([^"]+)"|([^\s(]+))',
        ir_text,
        flags=re.MULTILINE,
    ):
        symbol = match.group(1) or match.group(2)
        if re.search(r"_choose\.definition\.\d+$", symbol):
            symbols.append(symbol)
    return symbols


_DUPLICATE_DEFINITION_SOURCE = """\
def choose(value: int = 1) -> int:
    return value

saved = choose
print(saved())

def choose(value: int = 2) -> int:
    return value + 10

print(saved(), choose())
"""


def test_pcc1_duplicate_definition_ir_matches_host_shape(
    pcc1_self_host_binary,
    tmp_path,
):
    """Host/pcc1 keep two bodies and pcc1 executes both live bindings."""


    src = tmp_path / "duplicate_definition_shape.py"
    src.write_text(
        _DUPLICATE_DEFINITION_SOURCE,
        encoding="utf-8",
    )
    host_ir = tmp_path / "duplicate_definition.host.ll"
    pcc1_ir = tmp_path / "duplicate_definition.pcc1.ll"
    common = [
        "--python-libpython",
        "off",
        "--backend",
        "self",
        "--emit-llvm",
    ]
    for compiler, output in (
        ([sys.executable, "-m", "pcc"], host_ir),
        ([str(pcc1_self_host_binary)], pcc1_ir),
    ):
        result = subprocess.run(
            compiler + common + [str(output), str(src)],
            cwd=str(REPO_ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    host_text = host_ir.read_text(encoding="utf-8")
    pcc1_text = pcc1_ir.read_text(encoding="utf-8")
    verify_ir_text(host_text)
    verify_ir_text(pcc1_text)
    expected = [
        "user_duplicate_definition_shape_choose.definition.0",
        "user_duplicate_definition_shape_choose.definition.1",
    ]
    assert _duplicate_definition_symbols(host_text) == expected
    assert _duplicate_definition_symbols(pcc1_text) == expected

    cpython = _run_python(src)
    pcc1 = _compile_and_run_artifact_no_host(
        [str(pcc1_self_host_binary)],
        src,
        tmp_path / "duplicate_definition.pcc1.out",
    )
    assert cpython.returncode == 0
    assert pcc1.returncode == cpython.returncode
    assert cpython.stdout == "1\n1 12\n"
    assert pcc1.stdout == cpython.stdout
    assert pcc1.stderr == cpython.stderr


def test_pcc1_runtime_rebuild_uses_repo_python_and_surfaces_make_error(
    pcc1_self_host_binary,
    tmp_path,
):
    """A pcc1 app build never hides the causal runtime-build diagnostic.

    The synthetic runtime target deliberately imports llvmlite and then fails
    with a stable marker.  PATH begins with an executable python3 that cannot
    do that import; reaching the stable marker therefore proves pcc1 passed the
    repository .venv interpreter to Make.  The deliberate failure must stop at
    the runtime boundary rather than continuing to an unrelated undefined-
    symbol link error.
    """

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    causal_marker = "PCC_BLESSED_HOST_RUNTIME_FAILURE"
    (runtime_dir / "Makefile").write_text(
        "libpy_runtime_pcc_py.a:\n"
        "\t@$(PYTHON) -c \"import llvmlite; "
        f"raise SystemExit('{causal_marker}')\"\n",
        encoding="utf-8",
    )
    bad_bin = tmp_path / "unblessed-bin"
    bad_bin.mkdir()
    bad_python = bad_bin / "python3"
    bad_python.write_text(
        "#!/bin/sh\n"
        "echo PCC_UNBLESSED_PATH_PYTHON_USED >&2\n"
        "exit 97\n",
        encoding="utf-8",
    )
    bad_python.chmod(0o755)
    source = tmp_path / "app.py"
    source.write_text("print(42)\n", encoding="utf-8")
    output = tmp_path / "app"

    run_env = _child_env()
    run_env.pop("PCC_HOST_PYTHON", None)
    run_env.pop("PCC_RUNTIME_ARCHIVE", None)
    run_env["PATH"] = str(bad_bin) + os.pathsep + run_env.get("PATH", "")
    run_env["PCC_SOURCE_ROOT"] = str(REPO_ROOT)
    run_env["PCC_REPO_ROOT"] = str(REPO_ROOT)
    run_env["PCC_RUNTIME_DIR"] = str(runtime_dir)
    run_env["PCC_RUNTIME_CC"] = "pcc"
    result = subprocess.run(
        [
            str(pcc1_self_host_binary),
            "--python-libpython",
            "off",
            "--backend",
            "self",
            str(source),
            "-o",
            str(output),
        ],
        cwd=str(REPO_ROOT),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode != 0
    assert causal_marker in diagnostic
    assert "PCC_UNBLESSED_PATH_PYTHON_USED" not in diagnostic
    assert "failed to build required Python runtime archive" in diagnostic
    assert "undefined symbol" not in diagnostic.lower()


def test_pcc2_hoists_nested_closure_across_try_handler(
    pcc2_self_host_binary,
    tmp_path,
):
    src = tmp_path / "nested_closure_try.py"
    src.write_text(
        textwrap.dedent("""
            def locate(mod_name: str):
                def candidates(root: str) -> list[str]:
                    return [root + mod_name]

                try:
                    origin = candidates("root")[0]
                except Exception:
                    return None
                return origin

            print(locate("x"))
            """).lstrip(),
        encoding="utf-8",
    )
    result = _compile_and_run(
        [str(pcc2_self_host_binary)],
        src,
        tmp_path / "nested_closure_try.out",
    )
    assert result.returncode == 0
    assert result.stdout == "rootx\n"


def test_pcc1_bare_reraise_preserves_active_exception(
    pcc1_self_host_binary,
    tmp_path,
):
    src = tmp_path / "bare_reraise.py"
    src.write_text(
        textwrap.dedent("""
            def main() -> None:
                try:
                    try:
                        raise ValueError("inner")
                    except ValueError:
                        try:
                            try:
                                raise KeyError("nested")
                            except KeyError:
                                raise
                        except KeyError:
                            pass
                        raise
                except ValueError as exc:
                    print("outer caught", str(exc))

                try:
                    raise
                except RuntimeError as exc:
                    print("after handler", str(exc))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )

    cpython = _run_python(src)
    pcc1 = _compile_and_run_artifact_no_host(
        [str(pcc1_self_host_binary)],
        src,
        tmp_path / "bare_reraise.pcc1.out",
    )

    assert cpython.returncode == 0
    assert pcc1.returncode == cpython.returncode
    cpython_lines = cpython.stdout.splitlines()
    pcc1_lines = pcc1.stdout.splitlines()
    assert cpython_lines[0] == "outer caught inner"
    assert pcc1_lines[0] == cpython_lines[0]
    # The runtime's existing message starts with lower-case ``no`` while
    # CPython starts with ``No``.  This probe owns handler-stack cleanup, not
    # that separate diagnostic-capitalization parity boundary.
    assert pcc1_lines[1].lower() == cpython_lines[1].lower()
    assert pcc1.stderr == cpython.stderr


@pytest.mark.parametrize(
    "name, source",
    APPLICABLE_PARITY_CASES,
    ids=[name for name, _source in APPLICABLE_PARITY_CASES],
)
def test_pcc1_matches_stage0_for_python_idioms(
    pcc1_self_host_binary,
    tmp_path,
    request,
    name,
    source,
):
    src = tmp_path / f"{name}.py"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")

    stage0 = _compile_and_run_parity_observation(
        [sys.executable, "-m", "pcc"],
        src,
        tmp_path / f"{name}.stage0.out",
    )
    stage0_compile = stage0["compile"]
    assert isinstance(stage0_compile, dict)
    stage0_run = stage0["run"]
    pcc1 = None
    if (
        not stage0_compile["timed_out"]
        and stage0_compile["returncode"] == 0
        and isinstance(stage0_run, dict)
        and not stage0_run["timed_out"]
    ):
        pcc1 = _compile_and_run_parity_observation(
            [str(pcc1_self_host_binary)],
            src,
            tmp_path / f"{name}.pcc1.out",
        )
    problem = _parity_problem(stage0, pcc1)
    if problem is None:
        return

    _fail_host_pcc_pcc1_parity(
        request=request,
        name=name,
        src=src,
        pcc1_binary=pcc1_self_host_binary,
        host=stage0,
        pcc1=pcc1,
        problem=problem,
        manifest_path=PARITY_MANIFEST_PATH,
        candidate_names_digest=PARITY_APPLICABILITY.candidate_names_sha256,
    )


@pytest.mark.parametrize(
    "name, source",
    APPLICABLE_CORPUS_PARITY_CASES,
    ids=[name.replace("__", "/") for name, _source in APPLICABLE_CORPUS_PARITY_CASES],
)
def test_pcc1_matches_host_pcc_for_checked_python_corpus(
    pcc1_self_host_binary,
    tmp_path,
    request,
    name,
    source,
):
    case = CORPUS_CASE_BY_NAME[name]
    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    host = _compile_and_run_parity_observation(
        [sys.executable, "-m", "pcc"],
        src,
        tmp_path / f"{name}.host.out",
    )
    host_compile = host["compile"]
    host_run = host["run"]
    assert isinstance(host_compile, dict)
    pcc1 = None
    if (
        not host_compile["timed_out"]
        and host_compile["returncode"] == 0
        and isinstance(host_run, dict)
        and not host_run["timed_out"]
    ):
        pcc1 = _compile_and_run_parity_observation(
            [str(pcc1_self_host_binary)],
            src,
            tmp_path / f"{name}.pcc1.out",
        )
    problem = _parity_problem(host, pcc1)
    if problem is None:
        problem = _corpus_oracle_problem(host, case)
    if problem is None:
        return
    _fail_host_pcc_pcc1_parity(
        request=request,
        name=name,
        src=src,
        pcc1_binary=pcc1_self_host_binary,
        host=host,
        pcc1=pcc1,
        problem=problem,
        manifest_path=CORPUS_PARITY_MANIFEST_PATH,
        candidate_names_digest=CORPUS_PARITY_APPLICABILITY.candidate_names_sha256,
        oracle={
            "expected_status": case.expected_status,
            "expected_stdout_sha256": hashlib.sha256(
                case.expected_stdout
            ).hexdigest(),
        },
    )


@pytest.mark.parametrize(
    "name, source",
    APPLICABLE_INTENT_PARITY_CASES,
    ids=[name.replace("__", "/") for name, _source in APPLICABLE_INTENT_PARITY_CASES],
)
def test_pcc1_matches_host_pcc_for_intent_programs(
    pcc1_self_host_binary,
    tmp_path,
    request,
    name,
    source,
):
    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    host = _compile_and_run_parity_observation(
        [sys.executable, "-m", "pcc"],
        src,
        tmp_path / f"{name}.host.out",
    )
    host_compile = host["compile"]
    host_run = host["run"]
    assert isinstance(host_compile, dict)
    pcc1 = None
    if (
        not host_compile["timed_out"]
        and host_compile["returncode"] == 0
        and isinstance(host_run, dict)
        and not host_run["timed_out"]
    ):
        pcc1 = _compile_and_run_parity_observation(
            [str(pcc1_self_host_binary)],
            src,
            tmp_path / f"{name}.pcc1.out",
        )
    problem = _parity_problem(host, pcc1)
    if problem is None:
        return
    _fail_host_pcc_pcc1_parity(
        request=request,
        name=name,
        src=src,
        pcc1_binary=pcc1_self_host_binary,
        host=host,
        pcc1=pcc1,
        problem=problem,
        manifest_path=INTENT_PARITY_MANIFEST_PATH,
        candidate_names_digest=INTENT_PARITY_APPLICABILITY.candidate_names_sha256,
    )


@pytest.mark.parametrize(
    "name, source",
    (
        (
            "bitwise_int_ops_no_host",
            """
            def combine(a: int, b: int) -> None:
                print(a & b)
                print(a | b)
                print(a ^ b)
                print(~b)
                print(a << 2)
                print(a >> 1)

            def main() -> None:
                left = 6
                right = 3
                combine(left, right)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "bitwise_negative_shift_errors_no_host",
            """
            def main() -> None:
                try:
                    print(1 << -1)
                    print("left-missed")
                except ValueError:
                    print("left-error")
                try:
                    print(8 >> -2)
                    print("right-missed")
                except ValueError:
                    print("right-error")

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "getattr_default_and_if_args_or_kwargs_no_host",
            """
            class Node:
                def __init__(self) -> None:
                    self.tag = "value"

            def call_ident(expr):
                return getattr(expr, "tag", "missing")

            def classify(args, kwargs):
                if args or kwargs:
                    print("nonempty")
                else:
                    print("empty")

            def main() -> None:
                print(call_ident(Node()))
                print(call_ident(object()))

                classify([], {})
                classify(["x"], {})
                classify([], {"k": "v"})

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_keyword_arguments_prepare_new_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases, **kwargs):
                    LOG.append("prepare:" + kwargs["tag"])
                    return {}

                def __new__(mcls, name, bases, ns, **kwargs):
                    LOG.append("new:" + kwargs["tag"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.tag = kwargs["tag"]
                    return cls

            class Host(metaclass=Meta, tag="ready"):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(Host.tag + ":" + Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "user_instance_subscript_setitem_getitem_no_host",
            """
            class Box:
                def __init__(self) -> None:
                    self.data = {}
                    self.log = []

                def __setitem__(self, key, value) -> None:
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            def main() -> None:
                box = Box()
                box["k"] = "v"
                print(box["k"])
                print("|".join(box.log))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_data_descriptor_precedence_no_host",
            """
            class Descriptor:
                def __get__(self, obj, owner):
                    return "meta-get:" + obj.__name__ + ":" + owner.__name__

                def __set__(self, obj, value) -> None:
                    print("meta-set:" + obj.__name__ + ":" + value)

                def __delete__(self, obj) -> None:
                    print("meta-delete:" + obj.__name__)

            class Meta(type):
                label = Descriptor()

            class Host(metaclass=Meta):
                label = "class-label"

            def main() -> None:
                print(Host.label)
                Host.label = "next"
                print(Host.__dict__["label"])
                del Host.label
                print(Host.__dict__["label"])
                print(Host.label)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_property_readonly_precedence_no_host",
            """
            class Meta(type):
                @property
                def label(cls):
                    return "meta:" + cls.__name__

            class Host(metaclass=Meta):
                label = "class-label"

            def main() -> None:
                print(Host.label)
                try:
                    Host.label = "next"
                    print("set-missed")
                except AttributeError:
                    print("set-error")
                print(Host.__dict__["label"])
                try:
                    del Host.label
                    print("del-missed")
                except AttributeError:
                    print("del-error")
                print(Host.__dict__["label"])
                print(Host.label)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_runtime_class_object_property_precedence_no_host",
            """
            class Meta(type):
                @property
                def label(cls):
                    return "meta:" + cls.__name__

            class Host(metaclass=Meta):
                label = "class-label"

            def probe(cls) -> None:
                print(cls.label)
                try:
                    cls.label = "next"
                    print("set-missed")
                except AttributeError:
                    print("set-error")
                print(cls.__dict__["label"])
                try:
                    del cls.label
                    print("del-missed")
                except AttributeError:
                    print("del-error")
                print(cls.__dict__["label"])
                print(cls.label)

            def main() -> None:
                probe(Host)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_runtime_class_object_data_descriptor_precedence_no_host",
            """
            class Descriptor:
                def __get__(self, obj, owner):
                    return "meta-get:" + obj.__name__ + ":" + owner.__name__

                def __set__(self, obj, value) -> None:
                    print("meta-set:" + obj.__name__ + ":" + value)

                def __delete__(self, obj) -> None:
                    print("meta-delete:" + obj.__name__)

            class Meta(type):
                label = Descriptor()

            class Host(metaclass=Meta):
                label = "class-label"

            def probe(cls) -> None:
                print(cls.label)
                cls.label = "next"
                print(cls.__dict__["label"])
                del cls.label
                print(cls.__dict__["label"])
                print(cls.label)

            def main() -> None:
                probe(Host)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_runtime_class_object_property_setter_deleter_precedence_no_host",
            """
            class Meta(type):
                @property
                def label(cls):
                    return "meta:" + cls.state

                @label.setter
                def label(cls, value) -> None:
                    print("set:" + cls.__name__ + ":" + value)
                    cls.state = value

                @label.deleter
                def label(cls) -> None:
                    print("delete:" + cls.__name__)
                    cls.state = "deleted"

            class Host(metaclass=Meta):
                state = "start"
                label = "class-label"

            def probe(cls) -> None:
                print(cls.label)
                cls.label = "next"
                print(cls.label)
                print(cls.__dict__["label"])
                del cls.label
                print(cls.label)
                print(cls.__dict__["label"])

            def main() -> None:
                probe(Host)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_method_no_host",
            """
            class Base:
                def label(self):
                    return "base"

            class Child(Base):
                def label(self):
                    return super().label() + ":child"

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_classmethod_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is Child:
                        return "child"
                    return "wrong"

            class Child(Base):
                @classmethod
                def label(cls):
                    return super().label() + ":via-child"

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_nested_method_with_receiver_no_host",
            """
            class Base:
                def label(self):
                    return "base"

            class Child(Base):
                def label(self):
                    def inner(self):
                        return super().label()
                    return inner(self) + ":child"

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_nested_class_receiver_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is Grand:
                        return "grand"
                    if cls is Child:
                        return "child"
                    return "base"

            class Child(Base):
                @classmethod
                def label(cls):
                    def inner(cls):
                        return super().label()
                    return inner(cls) + ":via-child"

            class Grand(Child):
                pass

            def main() -> None:
                print(Grand.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_escaping_nested_class_receiver_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is Grand:
                        return "grand"
                    if cls is Child:
                        return "child"
                    return "base"

            class Child(Base):
                @classmethod
                def make(cls):
                    def inner(cls):
                        return super().label()
                    return inner

            class Grand(Child):
                pass

            def main() -> None:
                fn = Grand.make()
                print(fn(Grand))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_escaping_nested_method_receiver_no_host",
            """
            class Base:
                def label(self):
                    return "base:" + self.name

            class Child(Base):
                def __init__(self, name):
                    self.name = name

                def make(self):
                    def inner(self):
                        return super().label()
                    return inner

            def main() -> None:
                obj = Child("child")
                fn = obj.make()
                print(fn(obj))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "dunder_class_cell_method_no_host",
            """
            class Base:
                marker = "base"

            class Child(Base):
                marker = "child"

                def label(self):
                    cls = __class__
                    return cls.__name__ + ":" + cls.marker

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "dunder_class_cell_nested_method_no_host",
            """
            class Base:
                marker = "base"

            class Child(Base):
                marker = "child"

                def label(self):
                    def inner():
                        cls = __class__
                        return cls.__name__ + ":" + cls.marker
                    return inner()

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "dunder_class_cell_escaping_nested_method_no_host",
            """
            class Base:
                marker = "base"

            class Child(Base):
                marker = "child"

                def make(self):
                    def inner():
                        cls = __class__
                        return cls.__name__ + ":" + cls.marker
                    return inner

            def main() -> None:
                fn = Child().make()
                print(fn())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "dunder_class_local_shadow_escaping_nested_method_no_host",
            """
            class Base:
                marker = "base"

            class Child(Base):
                marker = "child"

                def make(self):
                    __class__ = "local"
                    def inner():
                        return "value:" + __class__
                    return inner

            def main() -> None:
                fn = Child().make()
                print(fn())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "dunder_class_cell_staticmethod_no_host",
            """
            class Base:
                marker = "base"

            class Child(Base):
                marker = "child"

                @staticmethod
                def label():
                    cls = __class__
                    return cls.__name__ + ":" + cls.marker

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "zero_arg_super_staticmethod_error_no_host",
            """
            class Base:
                @staticmethod
                def label():
                    return "base"

            class Child(Base):
                @staticmethod
                def label():
                    try:
                        return super().label()
                    except RuntimeError:
                        return "runtime-error"

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_body_dunder_class_nameerror_no_host",
            """
            RESULT = "unset"

            try:
                class Host:
                    value = __class__
                RESULT = "no-error"
            except NameError:
                RESULT = "name-error"

            def main() -> None:
                print(RESULT)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_method_no_host",
            """
            class Base:
                def label(self):
                    return "base"

            class Child(Base):
                def label(self):
                    return super(Child, self).label() + ":child"

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_classmethod_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is Child:
                        return "child"
                    return "wrong"

            class Child(Base):
                @classmethod
                def label(cls):
                    return super(Child, cls).label() + ":via-child"

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_invalid_receiver_typeerror_no_host",
            """
            class Base:
                def label(self):
                    return "base"

            class Child(Base):
                def label(self):
                    try:
                        return super(Child, Base()).label()
                    except TypeError:
                        return "type-error"

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_staticmethod_explicit_receiver_no_host",
            """
            class Base:
                def label(self):
                    return "base"

            class Child(Base):
                @staticmethod
                def label(obj):
                    return super(Child, obj).label() + ":child"

            def main() -> None:
                print(Child.label(Child()))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_class_receiver_subtype_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is GrandChild:
                        return "grand"
                    if cls is Child:
                        return "child"
                    return "wrong"

            class Child(Base):
                @staticmethod
                def label():
                    return super(Child, GrandChild).label()

            class GrandChild(Child):
                pass

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_class_alias_receiver_subtype_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is GrandChild:
                        return "grand"
                    if cls is Child:
                        return "child"
                    return "wrong"

            class Child(Base):
                @staticmethod
                def label():
                    return super(AliasChild, AliasGrand).label()

            class GrandChild(Child):
                pass

            AliasChild = Child
            AliasGrand = GrandChild

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_local_class_alias_receiver_subtype_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    if cls is GrandChild:
                        return "grand"
                    if cls is Child:
                        return "child"
                    return "wrong"

            class Child(Base):
                @staticmethod
                def label():
                    AliasChild = Child
                    AliasGrand = GrandChild
                    return super(AliasChild, AliasGrand).label()

            class GrandChild(Child):
                pass

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "argumented_super_missing_method_attributeerror_no_host",
            """
            class Base:
                def label(self):
                    return "base"

            class Child(Base):
                def label(self):
                    try:
                        return super(Child, self).missing()
                    except AttributeError:
                        return "attribute-error"

            def main() -> None:
                print(Child().label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "inherited_classmethod_cls_class_attr_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    return cls.name + ":base"

            class Child(Base):
                name = "child"

            def main() -> None:
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_mutation_visible_to_classmethod_no_host",
            """
            class Base:
                @classmethod
                def label(cls):
                    return cls.name + ":base"

            class Child(Base):
                name = "child"

            def main() -> None:
                print(Child.label())
                Child.name = "updated"
                print(Child.label())
                print(Child.name)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "inherited_classmethod_cls_attr_store_no_host",
            """
            class Base:
                name = "base"

                @classmethod
                def set_name(cls, value):
                    cls.name = value

                @classmethod
                def label(cls):
                    return cls.name + ":base"

            class Child(Base):
                name = "child"

            def main() -> None:
                print(Child.label())
                Child.set_name("updated")
                print(Child.label())
                print(Base.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_delete_visible_to_classmethod_no_host",
            """
            class Base:
                name = "base"

                @classmethod
                def label(cls):
                    return cls.name + ":base"

            class Child(Base):
                name = "child"

            def main() -> None:
                print(Child.label())
                del Child.name
                print(Child.label())
                print(Child.name)
                print(Base.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_descriptor_get_owner_no_host",
            """
            class Descriptor:
                def __get__(self, obj, owner):
                    if obj is None and owner is Child:
                        return "child-owner"
                    if obj is None and owner is Base:
                        return "base-owner"
                    return "instance"

            class Base:
                desc = Descriptor()

                @classmethod
                def label(cls):
                    return cls.desc + ":label"

            class Child(Base):
                pass

            def main() -> None:
                print(Base.desc)
                print(Child.desc)
                print(Child.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_staticmethod_classmethod_wrapper_access_no_host",
            """
            class Base:
                @staticmethod
                def marker(value):
                    return "static:" + value

                @classmethod
                def label(cls):
                    if cls is Child:
                        return "child"
                    return "base"

            class Child(Base):
                pass

            def main() -> None:
                static_fn = Child.marker
                class_fn = Child.label
                print(static_fn("x"))
                print(class_fn())
                print(Base.marker("y"))
                print(Base.label())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_instance_method_unbound_value_no_host",
            """
            class Base:
                def label(self, suffix):
                    return self.name + suffix

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                fn = Child.label
                print(fn(Child(), ":value"))
                print(Base.label(Child(), ":base"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "instance_method_bound_name_self_no_host",
            """
            class Child:
                def __init__(self) -> None:
                    self.name = "child"

                def label(self, suffix):
                    return self.name + suffix

            def main() -> None:
                obj = Child()
                m1 = obj.label
                m2 = obj.label
                print(m1 is m2)
                print(m1.__name__)
                print(m1.__self__ is obj)
                print(m1.__self__.name)
                print(m1(":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "dynamic_class_attr_function_instance_bound_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Child:
                def __init__(self) -> None:
                    self.name = "child"

                def label(self, suffix):
                    return self.name + suffix + ":old"

            def main() -> None:
                obj = Child()
                Child.label = replacement
                m1 = obj.label
                m2 = obj.label
                print(m1 is m2)
                print(m1.__name__)
                print(m1.__self__ is obj)
                print(m1.__self__.name)
                print(m1(":value"))
                print(Child.label.__name__)
                print(Child.label(obj, ":class"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_runtime_lookup_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":old"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                print(Child.label(Child(), ":before"))
                Child.label = replacement
                fn = Child.label
                print(fn(Child(), ":after"))
                print(Child.label(Child(), ":direct"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_delete_fallback_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                print(Child.label(Child(), ":set"))
                del Child.label
                fn = Child.label
                print(fn(Child(), ":value"))
                print(Child.label(Child(), ":direct"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_untaken_branch_fallback_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            def choose():
                return False

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                if choose():
                    Child.label = replacement
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_taken_branch_lookup_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            def choose():
                return True

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                if choose():
                    Child.label = replacement
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_loop_untaken_delete_preserves_replacement_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            def choose():
                return False

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                while choose():
                    del Child.label
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_loop_taken_delete_fallback_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                count = 0
                while count == 0:
                    del Child.label
                    count = 1
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_try_except_untaken_delete_preserves_replacement_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                try:
                    value = "safe"
                except ValueError:
                    del Child.label
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_try_except_taken_delete_fallback_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                try:
                    raise ValueError("boom")
                except ValueError:
                    del Child.label
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_finally_delete_fallback_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                try:
                    value = "safe"
                finally:
                    del Child.label
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_finally_store_after_delete_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                try:
                    del Child.label
                finally:
                    Child.label = replacement
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_loop_break_delete_fallback_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                while True:
                    del Child.label
                    break
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_method_replacement_loop_continue_skips_delete_no_host",
            """
            def replacement(self, suffix):
                return self.name + suffix + ":new"

            class Base:
                def label(self, suffix):
                    return self.name + suffix + ":base"

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                Child.label = replacement
                count = 0
                while count == 0:
                    count = 1
                    continue
                    del Child.label
                print(Child.label(Child(), ":direct"))
                fn = Child.label
                print(fn(Child(), ":value"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_descriptor_replacement_runtime_lookup_no_host",
            """
            class Descriptor:
                def __init__(self, tag):
                    self.tag = tag

                def __get__(self, obj, owner):
                    if obj is None and owner is Child:
                        return self.tag + ":child"
                    if obj is None and owner is Base:
                        return self.tag + ":base"
                    return self.tag + ":instance"

            class Base:
                desc = Descriptor("base")

                @classmethod
                def label(cls):
                    return cls.desc + ":label"

            class Child(Base):
                pass

            def main() -> None:
                print(Child.desc)
                Child.desc = Descriptor("new")
                print(Child.desc)
                print(Child.label())
                print(Base.desc)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_descriptor_replacement_delete_fallback_no_host",
            """
            class Descriptor:
                def __init__(self, tag):
                    self.tag = tag

                def __get__(self, obj, owner):
                    if obj is None and owner is Child:
                        return self.tag + ":child"
                    if obj is None and owner is Base:
                        return self.tag + ":base"
                    return self.tag + ":instance"

            class Base:
                desc = Descriptor("base")

                @classmethod
                def label(cls):
                    return cls.desc + ":label"

            class Child(Base):
                pass

            def main() -> None:
                Child.desc = Descriptor("new")
                print(Child.desc)
                del Child.desc
                print(Child.desc)
                print(Child.label())
                print(Base.desc)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_function_descriptor_identity_no_host",
            """
            class Base:
                def label(self, suffix):
                    return self.name + suffix

                @staticmethod
                def marker(value):
                    return "static:" + value

            class Child(Base):
                def __init__(self) -> None:
                    self.name = "child"

            def main() -> None:
                fn1 = Child.label
                fn2 = Child.label
                print(fn1 is fn2)
                print(fn1.__name__)
                print(fn1(Child(), ":value"))
                static1 = Child.marker
                static2 = Child.marker
                print(static1 is static2)
                print(static1.__name__)
                print(static1("x"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_classmethod_bound_name_identity_no_host",
            """
            class Base:
                name = "base"

                @classmethod
                def label(cls, suffix):
                    return cls.name + suffix

            class Child(Base):
                name = "child"

            def main() -> None:
                cm1 = Child.label
                cm2 = Child.label
                print(cm1 is cm2)
                print(cm1.__name__)
                print(cm1(":value"))
                print(Base.label.__name__)
                print(Base.label(":base"))

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "class_attr_classmethod_bound_self_no_host",
            """
            class Base:
                name = "base"

                @classmethod
                def label(cls):
                    return cls.name

            class Child(Base):
                name = "child"

            def main() -> None:
                cm = Child.label
                print(cm.__self__ is Child)
                print(cm.__self__.name)
                print(cm())
                base = Base.label
                print(base.__self__ is Base)
                print(base.__self__.name)
                print(base())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_setitem_order_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self):
                    self.data = {}
                    self.log = []

                def __setitem__(self, key, value):
                    self.log.append(key)
                    self.data[key] = value

                def __getitem__(self, key):
                    return self.data[key]

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return Namespace()

                def __new__(mcls, name, bases, ns):
                    LOG.append("|".join(ns.log))
                    return type.__new__(mcls, name, bases, ns.data)

            class Host(metaclass=Meta):
                value = "body"

                def method(self):
                    return self.value

            def main() -> None:
                obj = Host()
                print(LOG[0])
                print(obj.method())

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_getitem_new_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self):
                    self.data = {}
                    self.log = []

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return Namespace()

                def __new__(mcls, name, bases, ns):
                    value = ns["value"]
                    LOG.append(ns.log[len(ns.log) - 1])
                    cls = type.__new__(mcls, name, bases, ns.data)
                    cls.copied = value
                    return cls

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(Host.copied)
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_class_body_lookup_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self):
                    self.data = {}
                    self.log = []

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return Namespace()

                def __new__(mcls, name, bases, ns):
                    LOG.append("|".join(ns.log))
                    return type.__new__(mcls, name, bases, ns.data)

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_delete_name_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self):
                    self.data = {}
                    self.log = []

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

                def __delitem__(self, key):
                    self.log.append("del:" + key)
                    del self.data[key]

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return Namespace()

                def __new__(mcls, name, bases, ns):
                    LOG.append("|".join(ns.log))
                    return type.__new__(mcls, name, bases, ns.data)

            class Host(metaclass=Meta):
                value = "body"
                seen = value
                del value

            def main() -> None:
                print(LOG[0])
                print(Host.seen)
                try:
                    print(Host.value)
                except AttributeError:
                    print("value-missing")

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_constructor_args_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self, tag):
                    self.data = {}
                    self.log = ["init:" + tag]

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return Namespace("tag")

                def __new__(mcls, name, bases, ns):
                    value = ns["value"]
                    LOG.append("|".join(ns.log))
                    cls = type.__new__(mcls, name, bases, ns.data)
                    cls.copied = value
                    return cls

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(Host.copied)
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_factory_return_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self, tag):
                    self.data = {}
                    self.log = ["init:" + tag]

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            def make_namespace(tag):
                LOG.append("factory:" + tag)
                return Namespace(tag)

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return make_namespace("tag")

                def __new__(mcls, name, bases, ns):
                    value = ns["value"]
                    LOG.append("|".join(ns.log))
                    cls = type.__new__(mcls, name, bases, ns.data)
                    cls.copied = value
                    return cls

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(Host.copied)
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_alias_constructor_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self, tag):
                    self.data = {}
                    self.log = ["init:" + tag]

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            Ns = Namespace

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return Ns("tag")

                def __new__(mcls, name, bases, ns):
                    value = ns["value"]
                    LOG.append("|".join(ns.log))
                    cls = type.__new__(mcls, name, bases, ns.data)
                    cls.copied = value
                    return cls

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(Host.copied)
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_factory_local_return_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self, tag):
                    self.data = {}
                    self.log = ["init:" + tag]

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            def make_namespace(tag):
                LOG.append("factory:" + tag)
                ns = Namespace(tag)
                return ns

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return make_namespace("tag")

                def __new__(mcls, name, bases, ns):
                    value = ns["value"]
                    LOG.append("|".join(ns.log))
                    cls = type.__new__(mcls, name, bases, ns.data)
                    cls.copied = value
                    return cls

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(Host.copied)
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_prepare_custom_namespace_generic_mapping_factory_no_host",
            """
            LOG = []

            class Namespace:
                def __init__(self, tag):
                    self.data = {}
                    self.log = ["init:" + tag]

                def __setitem__(self, key, value):
                    self.log.append("set:" + key)
                    self.data[key] = value

                def __getitem__(self, key):
                    self.log.append("get:" + key)
                    return self.data[key]

            def make_namespace(tag):
                LOG.append("factory:" + tag)
                ns = Namespace(tag)
                if tag == "tag":
                    return ns
                return ns

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    return make_namespace("tag")

                def __new__(mcls, name, bases, ns):
                    value = ns["value"]
                    LOG.append("|".join(ns.log))
                    cls = type.__new__(mcls, name, bases, ns.data)
                    cls.copied = value
                    return cls

            class Host(metaclass=Meta):
                value = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(Host.copied)
                print(Host.value)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_function_return_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose():
                LOG.append("choose")
                return Meta

            class Host(metaclass=choose()):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_function_arg_return_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose(tag):
                LOG.append("choose:" + tag)
                return Meta

            class Host(metaclass=choose("arg")):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_conditional_return_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose(tag):
                LOG.append("choose:" + tag)
                if tag == "arg":
                    return Meta
                else:
                    return Meta

            class Host(metaclass=choose("arg")):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_conditional_expr_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose():
                LOG.append("choose")
                return False

            def select_then():
                LOG.append("then")
                return Meta

            def select_else():
                LOG.append("else")
                return Meta

            class Host(metaclass=select_then() if choose() else select_else()):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(LOG[3])
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_bool_or_expr_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose():
                LOG.append("choose")
                return Meta

            def fallback():
                LOG.append("fallback")
                return Meta

            class Host(metaclass=choose() or fallback()):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(len(LOG))
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_bool_and_expr_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose():
                LOG.append("choose")
                return Meta

            def fallback():
                LOG.append("fallback")
                return Meta

            class Host(metaclass=choose() and fallback()):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(LOG[3])
                print(len(LOG))
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_bool_or_falsey_left_expr_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            def choose_none():
                LOG.append("choose-none")
                return None

            def fallback():
                LOG.append("fallback")
                return Meta

            class Host(metaclass=choose_none() or fallback()):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(LOG[3])
                print(len(LOG))
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_bool_or_alias_fallback_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            AliasMeta = Meta

            def choose_none():
                LOG.append("choose-none")
                return None

            class Host(metaclass=choose_none() or AliasMeta):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(len(LOG))
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_bool_and_or_falsey_chain_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            AliasMeta = Meta

            def choose_none():
                LOG.append("choose-none")
                return None

            def fallback():
                LOG.append("fallback")
                return Meta

            class Host(metaclass=choose_none() and AliasMeta or fallback()):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(LOG[3])
                print(len(LOG))
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_dynamic_value_binding_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            chosen = Meta

            class Host(metaclass=chosen):
                kind = "body"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(Host.origin + ":" + Host.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_inherited_from_base_no_host",
            """
            LOG = []

            class Meta(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "meta"
                    return cls

            class Base(metaclass=Meta):
                kind = "base"

            class Child(Base):
                kind = "child"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(LOG[3])
                print(Child.origin + ":" + Child.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_compatible_bases_choose_most_derived_no_host",
            """
            LOG = []

            class MetaBase(type):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare-base:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new-base:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "base"
                    return cls

            class MetaDerived(MetaBase):
                @classmethod
                def __prepare__(mcls, name, bases):
                    LOG.append("prepare-derived:" + name)
                    return {}

                def __new__(mcls, name, bases, ns):
                    LOG.append("new-derived:" + name + ":" + ns["kind"])
                    cls = type.__new__(mcls, name, bases, ns)
                    cls.origin = "derived"
                    return cls

            class BaseA(metaclass=MetaBase):
                kind = "a"

            class BaseB(metaclass=MetaDerived):
                kind = "b"

            class Child(BaseA, BaseB):
                kind = "child"

            def main() -> None:
                print(LOG[0])
                print(LOG[1])
                print(LOG[2])
                print(LOG[3])
                print(LOG[4])
                print(LOG[5])
                print(Child.origin + ":" + Child.kind)

            if __name__ == "__main__":
                main()
            """,
        ),
        (
            "metaclass_conflict_between_bases_typeerror_no_host",
            """
            class MetaA(type):
                pass

            class MetaB(type):
                pass

            class BaseA(metaclass=MetaA):
                pass

            class BaseB(metaclass=MetaB):
                pass

            RESULT = "unset"
            try:
                class Bad(BaseA, BaseB):
                    pass
                RESULT = "no-error"
            except TypeError:
                RESULT = "type-error"

            def main() -> None:
                print(RESULT)

            if __name__ == "__main__":
                main()
            """,
        ),
    ),
)
def test_pcc1_no_host_matches_cpython_for_getattr_and_args_or_kwargs(
    pcc1_self_host_binary,
    tmp_path,
    name,
    source,
):
    src = tmp_path / f"{name}.py"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")

    cpython = _run_python(src)
    assert cpython.returncode == 0

    pcc1 = _compile_and_run_artifact_no_host(
        [str(pcc1_self_host_binary)],
        src,
        tmp_path / f"{name}.pcc1.out",
    )

    assert pcc1.returncode == cpython.returncode
    assert pcc1.stdout == cpython.stdout
    assert pcc1.stderr == cpython.stderr


def test_pcc2_pcc3_fixpoint_and_no_libpython(
    pcc1_self_host_binary,
    pcc2_self_host_binary,
    pcc3_self_host_binary,
    tmp_path,
):
    assert not _links_libpython(pcc1_self_host_binary)
    assert not _links_libpython(pcc2_self_host_binary)
    assert not _links_libpython(pcc3_self_host_binary)

    pcc2 = _signature_stripped_copy(
        pcc2_self_host_binary,
        tmp_path / "pcc2.nosig",
    )
    pcc3 = _signature_stripped_copy(
        pcc3_self_host_binary,
        tmp_path / "pcc3.nosig",
    )
    assert pcc2.read_bytes() == pcc3.read_bytes()


@pytest.mark.parametrize("name, source", FIXPOINT_CASES)
def test_pcc2_pcc3_match_stage0_for_smoke_idioms(
    pcc2_self_host_binary,
    pcc3_self_host_binary,
    tmp_path,
    name,
    source,
):
    src = tmp_path / f"{name}.py"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")

    stage0 = _compile_and_run(
        [sys.executable, "-m", "pcc"],
        src,
        tmp_path / f"{name}.stage0.out",
    )
    pcc2 = _compile_and_run(
        [str(pcc2_self_host_binary)],
        src,
        tmp_path / f"{name}.pcc2.out",
    )
    pcc3 = _compile_and_run(
        [str(pcc3_self_host_binary)],
        src,
        tmp_path / f"{name}.pcc3.out",
    )

    assert pcc2.returncode == stage0.returncode
    assert pcc2.stdout == stage0.stdout
    assert pcc2.stderr == stage0.stderr
    assert pcc3.returncode == stage0.returncode
    assert pcc3.stdout == stage0.stdout
    assert pcc3.stderr == stage0.stderr
