"""Finite, fail-closed native subset of :mod:`unittest.mock`.

Owned behavior is deliberately concrete: explicit-return/side-effect
``Mock`` calls, call recording/assertions, one target at a time through
``patch``/``patch.object``, and transactional patching of real dictionaries.
Automatic child mocks, magic methods, autospeccing, async mocks, mappings that
cannot provide transactional ``clear``/``update`` restoration, and
stacked/class patch decorators are not guessed.  They raise stable
``NotImplementedError`` boundaries instead.
"""
from __future__ import annotations

import importlib


__all__ = (
    "Mock",
    "MagicMock",
    "patch",
    "sentinel",
    "DEFAULT",
    "ANY",
    "call",
    "create_autospec",
    "AsyncMock",
    "ThreadingMock",
    "FILTER_DIR",
    "NonCallableMock",
    "NonCallableMagicMock",
    "mock_open",
    "PropertyMock",
    "seal",
)


FILTER_DIR = True
_MAX_TARGET_LENGTH = 4096
_MAX_TARGET_COMPONENTS = 128
_MAX_PATCH_DICT_ITEMS = 100000


class _SentinelValue:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "sentinel." + self.name


class _SentinelNamespace:
    def __init__(self):
        self.DEFAULT = _SentinelValue("DEFAULT")

    def __getattr__(self, name):
        raise NotImplementedError(
            "dynamic unittest.mock sentinel creation is not runtime-owned"
        )


sentinel = _SentinelNamespace()
DEFAULT = sentinel.DEFAULT


class _ANY:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __repr__(self):
        return "<ANY>"


ANY = _ANY()


class _Call:
    def __init__(self, args=(), kwargs=None):
        self.args = tuple(args)
        self.kwargs = {} if kwargs is None else dict(kwargs)

    def __len__(self):
        return 2

    def __iter__(self):
        return iter((self.args, self.kwargs))

    def __getitem__(self, index):
        if index == 0:
            return self.args
        if index == 1:
            return self.kwargs
        raise IndexError(index)

    def __eq__(self, other):
        if isinstance(other, _Call):
            return self.args == other.args and self.kwargs == other.kwargs
        if isinstance(other, tuple) and len(other) == 2:
            return self.args == other[0] and self.kwargs == other[1]
        return False

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        parts = []
        for value in self.args:
            parts.append(repr(value))
        for key, value in self.kwargs.items():
            parts.append(str(key) + "=" + repr(value))
        return "call(" + ", ".join(parts) + ")"


class _CallFactory:
    def __call__(self, *args, **kwargs):
        return _Call(args, kwargs)

    def __getattr__(self, name):
        raise NotImplementedError(
            "chained unittest.mock call construction is not runtime-owned"
        )


call = _CallFactory()


class _MockBase:
    """Shared explicit mock state without assuming callability."""

    def __init__(
        self,
        spec=None,
        side_effect=None,
        return_value=DEFAULT,
        wraps=None,
        name=None,
        spec_set=None,
        parent=None,
        _spec_state=None,
        _new_name="",
        _new_parent=None,
        unsafe=False,
        **kwargs,
    ):
        if spec is not None or spec_set is not None:
            raise NotImplementedError(
                "unittest.mock spec and spec_set are not runtime-owned"
            )
        if wraps is not None:
            raise NotImplementedError("unittest.mock wraps is not runtime-owned")
        if parent is not None or _spec_state is not None or _new_parent is not None:
            raise NotImplementedError(
                "unittest.mock child-parent graphs are not runtime-owned"
            )
        if _new_name != "":
            raise NotImplementedError(
                "unittest.mock automatic child names are not runtime-owned"
            )
        if unsafe:
            raise NotImplementedError(
                "unsafe unittest.mock attribute access is not runtime-owned"
            )
        self._mock_name = name
        self._return_value = return_value
        self.side_effect = side_effect
        self.called = False
        self.call_count = 0
        self.call_args = None
        self.call_args_list = []
        self.mock_calls = []
        self.method_calls = []
        for key, value in kwargs.items():
            if "." in key:
                raise NotImplementedError(
                    "dotted unittest.mock configuration is not runtime-owned"
                )
            setattr(self, key, value)

    def __getattr__(self, name):
        raise NotImplementedError(
            "automatic child attribute mocks are not runtime-owned: " + str(name)
        )

    def __repr__(self):
        if self._mock_name is None:
            return "<Mock>"
        return "<Mock name=" + repr(self._mock_name) + ">"

    @property
    def return_value(self):
        if self._return_value is DEFAULT:
            raise NotImplementedError(
                "automatic child return mocks are not runtime-owned; set an "
                "explicit return_value"
            )
        return self._return_value

    @return_value.setter
    def return_value(self, value):
        self._return_value = value

    def reset_mock(self, return_value=False, side_effect=False):
        self.called = False
        self.call_count = 0
        self.call_args = None
        self.call_args_list = []
        self.mock_calls = []
        self.method_calls = []
        if return_value:
            self._return_value = DEFAULT
        if side_effect:
            self.side_effect = None

    def configure_mock(self, **kwargs):
        for key, value in kwargs.items():
            if "." in key:
                raise NotImplementedError(
                    "dotted unittest.mock configuration is not runtime-owned"
                )
            setattr(self, key, value)

    def assert_called(self):
        if not self.called:
            raise AssertionError("Expected mock to have been called.")

    def assert_not_called(self):
        if self.called:
            raise AssertionError(
                "Expected mock to not have been called. Called "
                + str(self.call_count)
                + " times."
            )

    def assert_called_once(self):
        if self.call_count != 1:
            raise AssertionError(
                "Expected mock to have been called once. Called "
                + str(self.call_count)
                + " times."
            )

    def assert_called_with(self, *args, **kwargs):
        expected = _Call(args, kwargs)
        if self.call_args != expected:
            raise AssertionError(
                "expected call not found.\nExpected: "
                + repr(expected)
                + "\n  Actual: "
                + repr(self.call_args)
            )

    def assert_called_once_with(self, *args, **kwargs):
        self.assert_called_once()
        self.assert_called_with(*args, **kwargs)

    def assert_any_call(self, *args, **kwargs):
        expected = _Call(args, kwargs)
        if expected not in self.call_args_list:
            raise AssertionError(repr(expected) + " call not found")

    def assert_has_calls(self, calls, any_order=False):
        expected = list(calls)
        if any_order:
            remaining = list(self.mock_calls)
            for item in expected:
                if item not in remaining:
                    raise AssertionError("Calls not found: " + repr(expected))
                remaining.remove(item)
            return
        if len(expected) == 0:
            return
        if len(expected) > len(self.mock_calls):
            raise AssertionError("Calls not found: " + repr(expected))
        limit = len(self.mock_calls) - len(expected) + 1
        index = 0
        while index < limit:
            if self.mock_calls[index : index + len(expected)] == expected:
                return
            index += 1
        raise AssertionError("Calls not found: " + repr(expected))

    def mock_add_spec(self, spec, spec_set=False):
        raise NotImplementedError(
            "unittest.mock spec and spec_set are not runtime-owned"
        )

    def attach_mock(self, mock, attribute):
        raise NotImplementedError(
            "unittest.mock child-parent graphs are not runtime-owned"
        )


class Mock(_MockBase):
    """A callable recorder that requires an explicit result or side effect."""

    def __call__(self, *args, **kwargs):
        current = _Call(args, kwargs)
        self.called = True
        self.call_count += 1
        self.call_args = current
        self.call_args_list.append(current)
        self.mock_calls.append(current)

        effect = self.side_effect
        if effect is not None:
            if isinstance(effect, BaseException):
                raise effect
            if isinstance(effect, type):
                raise NotImplementedError(
                    "exception-class and other class mock side effects are not "
                    "runtime-owned"
                )
            if not callable(effect):
                raise NotImplementedError(
                    "iterable and exception-class mock side effects are not "
                    "runtime-owned"
                )
            result = effect(*args, **kwargs)
            if result is not DEFAULT:
                return result
        if self._return_value is DEFAULT:
            raise NotImplementedError(
                "automatic child return mocks are not runtime-owned; set an "
                "explicit return_value"
            )
        return self._return_value


class NonCallableMock(_MockBase):
    pass


def _magic_boundary(name):
    raise NotImplementedError(
        name + " requires automatic magic-method synthesis, which is not owned"
    )


class MagicMock(Mock):
    def __init__(self, *args, **kwargs):
        _magic_boundary("MagicMock")


class NonCallableMagicMock(NonCallableMock):
    def __init__(self, *args, **kwargs):
        _magic_boundary("NonCallableMagicMock")


class AsyncMock(Mock):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "AsyncMock coroutine scheduling and await accounting are not owned"
        )


class ThreadingMock(Mock):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "ThreadingMock wait/notification semantics are not runtime-owned"
        )


class PropertyMock(Mock):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "PropertyMock descriptor synthesis is not runtime-owned"
        )


def _validate_target(target):
    if not isinstance(target, str):
        raise TypeError("patch target must be a string")
    if target == "" or "\x00" in target or len(target) > _MAX_TARGET_LENGTH:
        raise ValueError("patch target must be a finite non-empty dotted name")
    parts = target.split(".")
    if len(parts) < 2 or len(parts) > _MAX_TARGET_COMPONENTS:
        raise ValueError("patch target must include a bounded module and attribute")
    for part in parts:
        if part == "":
            raise ValueError("patch target has an empty name component")
    return parts


def _resolve_target(target):
    parts = _validate_target(target)
    module = None
    module_end = len(parts) - 1
    while module_end > 0:
        module_name = ".".join(parts[:module_end])
        try:
            module = importlib.import_module(module_name)
            break
        except ImportError:
            module_end -= 1
    if module is None:
        raise ImportError("patch target module is not linked: " + target)
    owner = module
    index = module_end
    while index < len(parts) - 1:
        owner = getattr(owner, parts[index])
        index += 1
    return owner, parts[-1]


def _reject_stacked_or_class_decorator(function):
    if isinstance(function, type):
        raise NotImplementedError(
            "unittest.mock class decorators need reflective test discovery"
        )
    if getattr(function, "_pcc_mock_patch_decorated", False):
        raise NotImplementedError(
            "stacked unittest.mock decorators are not runtime-owned; nest "
            "explicit patch contexts instead"
        )


class _Patch:
    def __init__(
        self,
        target,
        attribute,
        new=DEFAULT,
        create=False,
        config=None,
    ):
        self.target = target
        self.attribute = attribute
        self.new = new
        self.create = create
        self.config = {} if config is None else dict(config)
        self._active = False
        self._had_original = False
        self._original = None
        self._replacement = None

    def _owner_and_attribute(self):
        if self.attribute is None:
            return _resolve_target(self.target)
        return self.target, self.attribute

    def __enter__(self):
        if self._active:
            raise RuntimeError("patcher is already active")
        owner, attribute = self._owner_and_attribute()
        had_visible = hasattr(owner, attribute)
        if not had_visible and not self.create:
            raise AttributeError(attribute)
        namespace = getattr(owner, "__dict__", None)
        had_local = False
        if namespace is not None and attribute in namespace:
            had_local = True
            original = namespace[attribute]
        elif had_visible:
            original = getattr(owner, attribute)
            # Slot-backed objects have no instance dictionary; assigning the
            # saved value through their descriptor is the only honest restore.
            had_local = namespace is None
        else:
            original = None
        replacement = self.new
        if replacement is DEFAULT:
            replacement = Mock(**self.config)
        elif self.config:
            raise TypeError("explicit patch replacements do not accept keywords")
        setattr(owner, attribute, replacement)
        self._owner = owner
        self._resolved_attribute = attribute
        self._had_original = had_visible
        self._had_local = had_local
        self._original = original
        self._replacement = replacement
        self._active = True
        return replacement

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._active:
            raise RuntimeError("patcher is not active")
        if self._had_original and self._had_local:
            setattr(self._owner, self._resolved_attribute, self._original)
        else:
            delattr(self._owner, self._resolved_attribute)
        self._active = False
        self._owner = None
        self._original = None
        self._replacement = None
        return False

    def start(self):
        return self.__enter__()

    def stop(self):
        self.__exit__(None, None, None)
        return None

    def __call__(self, function):
        _reject_stacked_or_class_decorator(function)
        patcher = self

        def decorated(*args, **kwargs):
            with patcher as replacement:
                if patcher.new is DEFAULT:
                    call_args = args + (replacement,)
                    return function(*call_args, **kwargs)
                return function(*args, **kwargs)

        setattr(decorated, "_pcc_mock_patch_decorated", True)
        return decorated


class _PatchDict:
    def __init__(self, in_dict, values=(), clear=False, **kwargs):
        self.in_dict = in_dict
        if isinstance(values, dict):
            self.values = dict(values)
        elif values == ():
            self.values = {}
        else:
            raise NotImplementedError(
                "patch.dict values must be a concrete dictionary"
            )
        self.values.update(kwargs)
        self.clear = clear
        self._active = False
        self._mapping = None
        self._original = None

    def _resolve_mapping(self):
        mapping = self.in_dict
        if isinstance(mapping, str):
            owner, attribute = _resolve_target(mapping)
            mapping = getattr(owner, attribute)
        if not hasattr(mapping, "clear") or not hasattr(mapping, "update"):
            raise NotImplementedError(
                "patch.dict requires a mutable mapping with clear/update for "
                "transactional restoration"
            )
        return mapping

    def __enter__(self):
        if self._active:
            raise RuntimeError("patcher is already active")
        mapping = self._resolve_mapping()
        try:
            original = dict(mapping)
        except Exception:
            raise NotImplementedError(
                "patch.dict requires a bounded mapping snapshot for "
                "transactional restoration"
            )
        if len(original) > _MAX_PATCH_DICT_ITEMS:
            raise RuntimeError("patch.dict mapping exceeds 100000 items")
        try:
            if self.clear:
                mapping.clear()
            mapping.update(self.values)
        except Exception:
            mapping.clear()
            mapping.update(original)
            raise
        self._mapping = mapping
        self._original = original
        self._active = True
        return mapping

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._active:
            raise RuntimeError("patcher is not active")
        self._mapping.clear()
        self._mapping.update(self._original)
        self._active = False
        self._mapping = None
        self._original = None
        return False

    def start(self):
        return self.__enter__()

    def stop(self):
        self.__exit__(None, None, None)
        return None

    def __call__(self, function):
        _reject_stacked_or_class_decorator(function)
        patcher = self

        def decorated(*args, **kwargs):
            with patcher:
                return function(*args, **kwargs)

        setattr(decorated, "_pcc_mock_patch_decorated", True)
        return decorated


class _PatchFacade:
    def __call__(
        self,
        target,
        new=DEFAULT,
        spec=None,
        create=False,
        spec_set=None,
        autospec=None,
        new_callable=None,
        **kwargs,
    ):
        if spec is not None or spec_set is not None or autospec is not None:
            raise NotImplementedError(
                "patch spec, spec_set, and autospec are not runtime-owned"
            )
        if new_callable is not None:
            raise NotImplementedError("patch new_callable is not runtime-owned")
        _validate_target(target)
        return _Patch(target, None, new, create, kwargs)

    def object(
        self,
        target,
        attribute,
        new=DEFAULT,
        spec=None,
        create=False,
        spec_set=None,
        autospec=None,
        new_callable=None,
        **kwargs,
    ):
        if not isinstance(attribute, str) or attribute == "":
            raise TypeError("patch.object attribute must be a non-empty string")
        if spec is not None or spec_set is not None or autospec is not None:
            raise NotImplementedError(
                "patch.object spec, spec_set, and autospec are not runtime-owned"
            )
        if new_callable is not None:
            raise NotImplementedError(
                "patch.object new_callable is not runtime-owned"
            )
        return _Patch(target, attribute, new, create, kwargs)

    def dict(self, in_dict, values=(), clear=False, **kwargs):
        return _PatchDict(in_dict, values, clear, **kwargs)

    def multiple(self, target, **kwargs):
        raise NotImplementedError(
            "patch.multiple atomic multi-target mutation is not runtime-owned"
        )

    def stopall(self):
        raise NotImplementedError(
            "patch.stopall global patch tracking is not runtime-owned"
        )


patch = _PatchFacade()


def create_autospec(spec, spec_set=False, instance=False, **kwargs):
    raise NotImplementedError("unittest.mock autospeccing is not runtime-owned")


def mock_open(mock=None, read_data=""):
    raise NotImplementedError(
        "unittest.mock file protocol and magic methods are not runtime-owned"
    )


def seal(mock_obj):
    raise NotImplementedError(
        "unittest.mock dynamic child-attribute sealing is not runtime-owned"
    )
