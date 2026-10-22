from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline import compile_python


@dataclass(frozen=True)
class FeatureCase:
    name: str
    source: str


SUPPORTED_CASES: tuple[FeatureCase, ...] = (
    FeatureCase(
        "literals_truthiness",
        """
        def main() -> None:
            print(None is None)
            print(True and not False)
            print(bool(0))
            print(bool("x"))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "arithmetic_and_comparison",
        """
        def main() -> None:
            print(2 + 3 * 4)
            print((20 - 5) // 3)
            print(17 % 5)
            print(1 < 2 < 3)
            print(3 > 2 >= 2)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "bitwise_shift_unary",
        """
        def main() -> None:
            x = 12
            y = 5
            print(x & y)
            print(x | y)
            print(x ^ y)
            print(x << 2)
            print(x >> 1)
            print(-y)
            print(+y)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "identity_and_none_compare",
        """
        def main() -> None:
            x = None
            y = []
            z = y
            print(x is None)
            print(y is z)
            print(y is not [])
            print(x == None)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "conditional_expression",
        """
        def pick(x):
            return "yes" if x else "no"

        def main() -> None:
            print(pick(True))
            print(pick(False))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "augassign_and_chained_assign",
        """
        def main() -> None:
            a = b = 3
            a += 4
            b *= 5
            print(a)
            print(b)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "membership_operators",
        """
        def main() -> None:
            print(2 in [1, 2, 3])
            print(4 not in [1, 2, 3])
            print("b" in "abc")
            print("x" not in {"a": 1})
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "float_arithmetic",
        """
        def main() -> None:
            x = 1.5
            y = 2.0
            print(x + y)
            print(y * 3.0)
            print(5 / 2)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "complex_numbers",
        """
        def main() -> None:
            z = 1 + 2j
            print(z.real)
            print(z.imag)
            c = complex(1, 2)
            print(c.real)
            print(c.imag)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "bytes_memoryview",
        """
        def main() -> None:
            b = b"abc"
            print(b[0])
            print(memoryview(b)[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "bytes_bytearray_basics",
        """
        def main() -> None:
            b = b"abc"
            print(b[1])
            print(len(b))
            ba = bytearray(b)
            ba[0] = 65
            print(bytes(ba).decode())
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "match_statement",
        """
        def main() -> None:
            value = ("ok", 3)
            match value:
                case ("ok", n):
                    print(n)
                case _:
                    print("miss")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "bool_short_circuit",
        """
        def side(label):
            print(label)
            return True

        def main() -> None:
            print(False and side("bad_and"))
            print(True or side("bad_or"))
            print(True and side("ok_and"))
            print(False or side("ok_or"))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "if_while_break_continue",
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
    FeatureCase(
        "while_else_break",
        """
        def scan(limit):
            i = 0
            while i < 4:
                if i == limit:
                    print("break")
                    break
                i = i + 1
            else:
                print("else")

        def main() -> None:
            scan(2)
            scan(9)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "pass_and_empty_blocks",
        """
        def main() -> None:
            if True:
                pass
            else:
                pass
            print("ok")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "range_start_stop_step",
        """
        def main() -> None:
            total = 0
            for i in range(2, 8, 2):
                total = total + i
            print(total)
            total2 = 0
            for i in range(5, 0, -2):
                total2 = total2 + i
            print(total2)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "for_range_list_tuple_dict_str",
        """
        def main() -> None:
            total = 0
            for i in range(4):
                total = total + i
            for x in [5, 6]:
                total = total + x
            pair = (7, 8)
            for x in pair:
                total = total + x
            d = {"a": 9, "b": 10}
            for k in d:
                total = total + d[k]
            chars = ""
            for ch in "ab":
                chars = chars + ch
            print(total)
            print(chars)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "for_else_break",
        """
        def scan(limit):
            for i in range(4):
                if i == limit:
                    print("break")
                    break
            else:
                print("else")

        def main() -> None:
            scan(2)
            scan(9)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "enumerate_zip_loop",
        """
        def main() -> None:
            xs = [3, 4, 5]
            total = 0
            for i, x in enumerate(xs):
                total = total + i * x
            print(total)
            ys = [10, 20, 30]
            total2 = 0
            for a, b in zip(xs, ys):
                total2 = total2 + a + b
            print(total2)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "enumerate_start",
        """
        def main() -> None:
            total = 0
            for i, x in enumerate([10, 20], start=5):
                total = total + i + x
            print(total)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "functions_defaults_kwargs_varargs",
        """
        def f(a, b=2, *args, scale=1):
            total = a + b
            for x in args:
                total = total + x
            return total * scale

        def main() -> None:
            print(f(1))
            print(f(1, 3, 5, 7, scale=2))
            print(f(a=4, b=6))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "keyword_only_and_kwargs",
        """
        def f(a, *, b=2, **kw):
            print(a + b)
            print(kw["x"])

        def main() -> None:
            f(3, b=4, x=5)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "positional_only",
        """
        def f(a, /, b):
            return a + b

        def main() -> None:
            print(f(2, b=3))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "recursive_function",
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
    FeatureCase(
        "global_variable_write",
        """
        value = 1

        def bump():
            global value
            value = value + 2

        def main() -> None:
            bump()
            print(value)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "delete_name_and_item",
        """
        def main() -> None:
            x = 3
            print(x)
            del x
            xs = [1, 2, 3]
            del xs[1]
            print(len(xs))
            print(xs[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "lambda_and_closure_rebind",
        """
        def make_counter(step):
            value = 0
            def inc():
                nonlocal value
                value = value + step
                return value
            return inc

        def main() -> None:
            f = make_counter(3)
            print(f())
            print(f())
            g = lambda x, y: x + y
            print(g(4, 5))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "classes_methods_inheritance",
        """
        class A:
            def f(self):
                return 5

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
    FeatureCase(
        "class_dunder_str",
        """
        class Label:
            def __str__(self):
                return "label-value"

        def main() -> None:
            print(str(Label()))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "staticmethod_classmethod_property",
        """
        class Box:
            scale = 2
            def __init__(self, value):
                self.value = value
            @staticmethod
            def add(a, b):
                return a + b
            @classmethod
            def scaled(cls, value):
                return value * cls.scale
            @property
            def doubled(self):
                return self.value * 2

        def main() -> None:
            b = Box(7)
            print(Box.add(2, 3))
            print(Box.scaled(5))
            print(b.doubled)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "class_attr_and_instance_attr",
        """
        class C:
            scale = 3
            def __init__(self, value):
                self.value = value
            def calc(self):
                self.value = self.value + 1
                return self.value * self.scale

        def main() -> None:
            c = C(4)
            print(c.calc())
            print(C.scale)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "dunder_len_iter_getitem",
        """
        class Seq:
            def __init__(self):
                self.xs = [4, 5, 6]
            def __len__(self):
                return len(self.xs)
            def __getitem__(self, idx):
                return self.xs[idx]

        def main() -> None:
            s = Seq()
            print(len(s))
            print(s[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "callable_instance",
        """
        class Adder:
            def __init__(self, amount):
                self.amount = amount
            def __call__(self, value):
                return value + self.amount

        def main() -> None:
            add3 = Adder(3)
            print(add3(7))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "list_tuple_unpack_slice_mutation",
        """
        def main() -> None:
            xs = [1, 2, 3]
            xs.append(4)
            xs[1:3] = [20, 30]
            a, b = (xs[1], xs[3])
            print(len(xs))
            print(a)
            print(b)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "negative_index_and_slices",
        """
        def main() -> None:
            xs = [1, 2, 3, 4]
            print(xs[-1])
            print(xs[1:3][0])
            print(xs[:2][1])
            print(xs[2:][0])
            print("abcd"[-1])
            print("abcd"[1:3])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "slice_step",
        """
        def main() -> None:
            xs = [1, 2, 3, 4, 5]
            ys = xs[::2]
            print(ys[0])
            print(ys[1])
            print(ys[2])
            print("abcdef"[::2])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "tuple_list_constructors",
        """
        def main() -> None:
            xs = list((1, 2, 3))
            ys = tuple([4, 5])
            print(xs[1])
            print(ys[0])
            print(len(ys))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "tuple_nested_unpack",
        """
        def main() -> None:
            a, (b, c) = (1, (2, 3))
            print(a)
            print(b)
            print(c)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "dict_set_membership",
        """
        def main() -> None:
            d = {"x": 1}
            d["y"] = 2
            s = {1, 2, 3}
            print(d["x"] + d["y"])
            print("y" in d)
            print(2 in s)
            print(5 in s)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "dict_methods",
        """
        def main() -> None:
            d = {"a": 1}
            print(d.get("a"))
            print(d.get("b", 9))
            print("a" in d.keys())
            print(1 in d.values())
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "set_operations",
        """
        def main() -> None:
            a = {1, 2}
            b = {2, 3}
            print(1 in a)
            print(3 in a)
            a.update(b)
            print(len(a))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "string_methods_and_fstring",
        """
        def main() -> None:
            text = "a,b,c"
            xs = text.split(",")
            print("-".join(xs))
            print(text.replace(",", ":"))
            n = 7
            print(f"n={n}")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "string_index_slice_ord_chr",
        """
        def main() -> None:
            s = "abcd"
            print(s[1])
            print(s[1:3])
            print(ord("A"))
            print(chr(66))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "string_predicates_and_strip",
        """
        def main() -> None:
            s = "  Abc123  "
            print(s.strip())
            print("123".isdigit())
            print("abc".isalpha())
            print("a1".isalnum())
            print("   ".isspace())
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "string_find_count_starts_ends",
        """
        def main() -> None:
            s = "banana"
            print(s.find("na"))
            print(s.count("na"))
            print(s.startswith("ba"))
            print(s.endswith("na"))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "hash_id_builtins",
        """
        def main() -> None:
            x = "abc"
            print(isinstance(hash(x), int))
            print(isinstance(id(x), int))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "list_comprehension",
        """
        def main() -> None:
            xs = [1, 2, 3, 4]
            ys = [x * 2 for x in xs if x > 2]
            print(ys[0])
            print(ys[1])
            print(len(ys))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "dict_set_comprehension",
        """
        def main() -> None:
            d = {x: x * 2 for x in [1, 2, 3] if x > 1}
            s = {x + 1 for x in [1, 2, 3]}
            print(d[2])
            print(d[3])
            print(4 in s)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "nested_list_comprehension",
        """
        def main() -> None:
            xs = [(a, b) for a in [1, 2] for b in [3, 4] if a + b > 4]
            print(len(xs))
            print(xs[0][0])
            print(xs[0][1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "generator_expression_sum",
        """
        def main() -> None:
            print(sum(x * 2 for x in [1, 2, 3]))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "exceptions_try_except",
        """
        def f(n):
            try:
                if n == 0:
                    raise ValueError("zero")
                return n
            except ValueError as e:
                print(type(e).__name__)
                return 42

        def main() -> None:
            print(f(0))
            print(f(5))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "exception_else_and_reraise",
        """
        def f(n):
            try:
                if n == 0:
                    raise ValueError("zero")
            except ValueError:
                print("except")
            else:
                print("else")

        def main() -> None:
            f(0)
            f(1)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "multiple_except",
        """
        def f(kind):
            try:
                if kind == 1:
                    raise ValueError("v")
                if kind == 2:
                    raise TypeError("t")
                return "ok"
            except ValueError:
                return "value"
            except TypeError:
                return "type"

        def main() -> None:
            print(f(1))
            print(f(2))
            print(f(3))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "bare_reraise",
        """
        def main() -> None:
            try:
                try:
                    raise ValueError("x")
                except ValueError:
                    print("inner")
                    raise
            except ValueError:
                print("outer")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "assert_true",
        """
        def main() -> None:
            assert 1 < 2
            print("ok")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "generator_next_yield_from",
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
    FeatureCase(
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
    FeatureCase(
        "builtin_min_max_sum_any_all",
        """
        def main() -> None:
            xs = [1, 2, 3]
            print(min(5, 2))
            print(max(5, 2))
            print(sum(xs))
            print(any([False, True]))
            print(all([True, True]))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "multifor_list_comprehension",
        """
        def main() -> None:
            print([x + y for x in [1, 2] for y in [10, 20]])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "comprehension_if_else_expr",
        """
        def main() -> None:
            print([x if x % 2 else -x for x in [1, 2, 3]])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "extended_unpack_call_return",
        """
        def f():
            return (1, 2, 3)

        def main() -> None:
            a, b, c = f()
            print(a + b + c)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "custom_exception_class",
        """
        class MyError(Exception):
            pass

        def main() -> None:
            try:
                raise MyError("bad")
            except MyError as e:
                print("caught")
                print(str(e))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "raise_from_cause",
        """
        def main() -> None:
            try:
                try:
                    raise ValueError("inner")
                except ValueError as e:
                    raise RuntimeError("outer") from e
            except RuntimeError as e:
                print(type(e.__cause__).__name__)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "assert_false_message",
        """
        def main() -> None:
            try:
                assert False, "boom"
            except AssertionError as e:
                print(str(e))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "super_call",
        """
        class A:
            def f(self):
                return 2

        class B(A):
            def f(self):
                return super().f() + 3

        def main() -> None:
            print(B().f())
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "inherited_method_no_init",
        """
        class A:
            def f(self):
                return 5

        class B(A):
            pass

        def main() -> None:
            print(B().f())
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "isinstance_user_class",
        """
        class A:
            pass

        class B(A):
            pass

        def main() -> None:
            b = B()
            print(isinstance(b, B))
            print(isinstance(b, A))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "property_setter",
        """
        class Box:
            def __init__(self):
                self._x = 0

            @property
            def x(self):
                return self._x

            @x.setter
            def x(self, v):
                self._x = v

        def main() -> None:
            b = Box()
            b.x = 4
            print(b.x)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "walrus_operator",
        """
        def main() -> None:
            if (n := 3) > 2:
                print(n)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "import_sys_argv",
        """
        import sys

        def main() -> None:
            print(len(sys.argv) >= 1)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "import_os_path",
        """
        import os

        def main() -> None:
            print(os.path.basename("/tmp/x"))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "import_dataclasses",
        """
        from dataclasses import dataclass

        @dataclass
        class P:
            x: int

        def main() -> None:
            print(P(3).x)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "container_deep_equality",
        """
        def main() -> None:
            print([1, 2] == [1, 2])
            print((1, 2) != (2, 1))
            print({"a": 1} == {"a": 1})
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "list_methods",
        """
        def main() -> None:
            xs = [3, 1]
            xs.extend([2])
            xs.insert(1, 4)
            print(xs.pop())
            print(xs.index(4))
            xs.remove(4)
            xs.sort()
            print(xs[0])
            print(xs[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "dict_pop_setdefault_items",
        """
        def main() -> None:
            d = {"a": 1}
            print(d.setdefault("b", 2))
            print(d.pop("a"))
            total = 0
            for k, v in d.items():
                total = total + v
            print(total)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "builtin_sorted_reversed",
        """
        def main() -> None:
            xs = sorted([3, 1, 2])
            print(xs[0])
            print(xs[2])
            ys = list(reversed(xs))
            print(ys[0])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "power_floor_div_mod",
        """
        def main() -> None:
            print(2 ** 5)
            print(17 // 3)
            print(17 % 3)
            print(divmod(17, 3)[0])
            print(divmod(17, 3)[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "issubclass_user_class",
        """
        class A:
            pass

        class B(A):
            pass

        def main() -> None:
            print(issubclass(B, A))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "inherited_init",
        """
        class A:
            def __init__(self, value):
                self.value = value
            def f(self):
                return self.value

        class B(A):
            def g(self):
                return self.f() + 2

        def main() -> None:
            b = B(5)
            print(b.g())
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "repr_and_type_name",
        """
        def main() -> None:
            x = "abc"
            print(repr(x))
            print(type(x).__name__)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "import_math_alias",
        """
        import math as m
        from math import sqrt

        def main() -> None:
            print(int(m.floor(3.9)))
            print(int(sqrt(9)))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "import_re_match",
        """
        import re
        from re import match

        def main() -> None:
            print(re.match("a+", "aa") is not None)
            print(match("\\\\d+", "123abc") is not None)
            print(re.match("z+", "aa") is None)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "function_annotations_runtime",
        """
        def f(x: int) -> int:
            return x + 1

        def main() -> None:
            print(f(2))
            print("x" in f.__annotations__)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "try_finally",
        """
        def f(n):
            try:
                if n == 0:
                    raise ValueError("zero")
                return n
            except ValueError:
                return 42
            finally:
                print("finally")

        def main() -> None:
            print(f(0))
            print(f(5))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "star_args_call",
        """
        def f(a, b, c=0):
            return a + b + c

        def main() -> None:
            xs = [1, 2]
            print(f(*xs, c=3))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "star_kwargs_call",
        """
        def f(a, b=0):
            return a + b

        def main() -> None:
            kw = {"b": 5}
            print(f(4, **kw))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "starred_assignment",
        """
        def main() -> None:
            a, *rest, b = [1, 2, 3, 4]
            print(a)
            print(rest)
            print(b)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "with_context_manager",
        """
        class C:
            def __enter__(self):
                print("enter")
                return 3
            def __exit__(self, exc_type, exc, tb):
                print("exit")
                return False

        def main() -> None:
            with C() as value:
                print(value)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "with_builtin_open",
        """
        def main() -> None:
            with open(__file__, "r") as f:
                s = f.read(1)
            print(len(s))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "builtin_map_filter",
        """
        def inc(x):
            return x + 1
        def keep(x):
            return x > 1

        def main() -> None:
            xs = list(map(inc, [1, 2]))
            ys = list(filter(keep, xs))
            print(xs[0])
            print(ys[0])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "string_format_method",
        """
        def main() -> None:
            print("{}:{}".format("x", 3))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "fstring_format_spec",
        """
        def main() -> None:
            x = 3.14159
            print(f"{x:.2f}")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "fstring_hex_format_spec",
        """
        def main() -> None:
            print(f"{255:x}")
            print(f"{255:04x}")
            print(f"{65535:08x}")
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "str_encode_latin1",
        """
        def main() -> None:
            b = "A\\xff".encode("latin-1")
            print(len(b))
            print(b[0])
            print(b[1])
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "getattr_setattr_delattr",
        """
        class A:
            pass

        def main() -> None:
            a = A()
            setattr(a, "x", 4)
            print(getattr(a, "x"))
            print(hasattr(a, "x"))
            delattr(a, "x")
            print(hasattr(a, "x"))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "object_dunder_setattr",
        """
        class Box:
            def __init__(self):
                self.value = "before"

            def normalize(self):
                object.__setattr__(self, "value", "after")

        def main() -> None:
            box = Box()
            box.normalize()
            print(box.value)
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "decorator_application",
        """
        def deco(fn):
            def wrapper(x):
                return fn(x) + 1
            return wrapper

        @deco
        def f(x):
            return x * 2

        def main() -> None:
            print(f(5))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "generator_send",
        """
        def echo():
            x = yield 0
            yield x

        def main() -> None:
            g = echo()
            print(next(g))
            print(g.send(10))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "generator_throw_close",
        """
        def gen():
            try:
                yield 1
            except ValueError:
                yield 2

        def main() -> None:
            g = gen()
            print(next(g))
            print(g.throw(ValueError("boom")))
        if __name__ == "__main__":
            main()
        """,
    ),
    FeatureCase(
        "async_def_coroutine_shell",
        """
        async def f():
            print("body-ran")
            return 1

        def main() -> None:
            c = f()
            print(type(c).__name__)
            c.close()
            print("ok")
        if __name__ == "__main__":
            main()
        """,
    ),
)


EXPECTED_GAPS: tuple[object, ...] = ()


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _run_cpython(src: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(src)],
        cwd=str(src.parent),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _compile_and_run_pcc(
    src: Path, out: Path,
) -> subprocess.CompletedProcess[str]:
    compile_python(
        str(src),
        str(out),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    return subprocess.run(
        [str(out)],
        cwd=str(src.parent),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _write_case(tmp_path: Path, case: FeatureCase) -> Path:
    src = tmp_path / f"{case.name}.py"
    src.write_text(textwrap.dedent(case.source).lstrip(), encoding="utf-8")
    return src


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=lambda c: c.name)
def test_supported_python_features_match_cpython(
    tmp_path: Path, case: FeatureCase,
) -> None:
    src = _write_case(tmp_path, case)
    expected = _run_cpython(src)
    assert expected.returncode == 0, expected.stderr

    actual = _compile_and_run_pcc(src, tmp_path / f"{case.name}.out")

    assert actual.returncode == expected.returncode
    assert actual.stdout == expected.stdout
    assert actual.stderr == expected.stderr


if EXPECTED_GAPS:
    @pytest.mark.parametrize("case", EXPECTED_GAPS, ids=lambda c: c.name)
    def test_known_python_feature_gaps_are_tracked_against_cpython(
        tmp_path: Path, case: FeatureCase,
    ) -> None:
        src = _write_case(tmp_path, case)
        expected = _run_cpython(src)
        assert expected.returncode == 0, expected.stderr

        actual = _compile_and_run_pcc(src, tmp_path / f"{case.name}.out")

        assert actual.returncode == expected.returncode
        assert actual.stdout == expected.stdout
        assert actual.stderr == expected.stderr
