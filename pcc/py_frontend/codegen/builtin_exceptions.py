"""Shared builtin exception tag metadata for Python lowering."""

from __future__ import annotations

BUILTIN_EXC_TAG = {
    "BaseException": 0,
    "Exception": 1,
    "ValueError": 2,
    "TypeError": 3,
    "KeyError": 4,
    "IndexError": 5,
    "AttributeError": 6,
    "SyntaxError": 1,
    "RuntimeError": 7,
    "StopIteration": 8,
    "ZeroDivisionError": 9,
    "NameError": 10,
    "NotImplementedError": 11,
    "ArithmeticError": 12,
    "LookupError": 13,
    "OSError": 14,
    "IOError": 14,
    "OverflowError": 15,
    "AssertionError": 16,
    "ReferenceError": 18,
    "MemoryError": 19,
    "FileNotFoundError": 14,
    "FileExistsError": 14,
    "IsADirectoryError": 14,
    "NotADirectoryError": 14,
    "PermissionError": 14,
    "BrokenPipeError": 14,
    "ConnectionError": 14,
    "ConnectionAbortedError": 14,
    "ConnectionRefusedError": 14,
    "ConnectionResetError": 14,
    "BlockingIOError": 14,
    "ChildProcessError": 14,
    "InterruptedError": 14,
    "TimeoutError": 14,
    "UnicodeError": 2,
    "UnicodeDecodeError": 2,
    "UnicodeEncodeError": 2,
    "RecursionError": 7,
    "ImportError": 20,
    "ModuleNotFoundError": 21,
    "EOFError": 1,
    "SystemExit": 0,
    "KeyboardInterrupt": 0,
    "GeneratorExit": 0,
    "StopAsyncIteration": 17,
    # Each warning class has its own identity. The runtime parent table makes
    # the subclasses match Warning without making sibling handlers match.
    "Warning": 22,
    "UserWarning": 23,
    "DeprecationWarning": 24,
    "PendingDeprecationWarning": 25,
    "SyntaxWarning": 26,
    "RuntimeWarning": 27,
    "FutureWarning": 28,
    "ImportWarning": 29,
    "UnicodeWarning": 30,
    "BytesWarning": 31,
    "EncodingWarning": 32,
    "ResourceWarning": 33,
}


def builtin_exc_tag_or_missing(name: str) -> int:
    if name in BUILTIN_EXC_TAG:
        return BUILTIN_EXC_TAG[name]
    return -1
