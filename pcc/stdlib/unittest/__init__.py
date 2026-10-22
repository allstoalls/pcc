"""Finite native :mod:`unittest` support for pcc-owned test sources.

This module deliberately separates assertion semantics from runner semantics.
The ordinary :class:`TestCase` assertions used by build-tool self tests are
native and deterministic.  Reflective discovery, result-driven execution,
subtest continuation, asynchronous cases, warning/log capture, and expected
failure accounting are not approximated: those entry points fail closed with
``NotImplementedError``.  A skipped class or function carries CPython's
``__unittest_skip__``/``__unittest_skip_why__`` marks; a skipped function
raises SkipTest when called.

For a top-level ``unittest.TestCase`` class followed by a literal, no-argument
``unittest.main()`` call, the compiler owns a small static runner.  That runner
directly invokes source-order ``test*`` methods, leaving out a class marked
skipped and counting a SkipTest as a skip; it does not imply ownership of
``setUp``/``tearDown``, result objects, or discovery.  It is intentionally not
exposed here as a general discovery API.
"""
from __future__ import annotations

import functools
import re


__unittest = True

__all__ = [
    "TestResult",
    "TestCase",
    "IsolatedAsyncioTestCase",
    "TestSuite",
    "TextTestRunner",
    "TestLoader",
    "FunctionTestCase",
    "main",
    "defaultTestLoader",
    "SkipTest",
    "skip",
    "skipIf",
    "skipUnless",
    "expectedFailure",
    "TextTestResult",
    "installHandler",
    "registerResult",
    "removeResult",
    "removeHandler",
    "addModuleCleanup",
    "doModuleCleanups",
    "enterModuleContext",
]


_MISSING = object()
_RUNNER_BOUNDARY = (
    "reflective unittest discovery and result-driven execution are not "
    "runtime-owned; use a top-level no-argument unittest.main() static runner"
)


def _validate_expected_exception(expected):
    if isinstance(expected, type):
        if not issubclass(expected, BaseException):
            raise TypeError("expected exception must derive from BaseException")
        return
    if isinstance(expected, tuple):
        if len(expected) > 128:
            raise NotImplementedError(
                "assertRaises exception tuples are bounded to 128 types"
            )
        for item in expected:
            if not isinstance(item, type) or not issubclass(item, BaseException):
                raise TypeError("expected exception must derive from BaseException")
        return
    raise TypeError("expected exception must be an exception type or tuple")


class SkipTest(Exception):
    """Raised by finite skip decorators and explicit test skips."""

    pass


class _AssertRaisesContext:
    def __init__(self, expected, test_case, expected_regex=None, message=None):
        self.expected = expected
        self.test_case = test_case
        self.expected_regex = expected_regex
        self.message = message
        self.exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.test_case._fail(
                "exception " + str(self.expected) + " not raised",
                self.message,
            )
        if not isinstance(exc_value, self.expected):
            return False
        if self.expected_regex is not None:
            if not isinstance(self.expected_regex, str):
                raise NotImplementedError(
                    "compiled regular expressions in assertRaisesRegex are "
                    "not runtime-owned"
                )
            if re.search(self.expected_regex, str(exc_value)) is None:
                self.test_case._fail(
                    "\""
                    + self.expected_regex
                    + "\" does not match \""
                    + str(exc_value)
                    + "\"",
                    self.message,
                )
        self.exception = exc_value
        return True


class TestCase:
    """Assertion-capable case object with no reflective run lifecycle."""

    failureException = AssertionError
    longMessage = True
    maxDiff = 640

    def __init__(self, methodName="runTest"):
        self._testMethodName = methodName

    def setUp(self):
        return None

    def tearDown(self):
        return None

    @classmethod
    def setUpClass(cls):
        return None

    @classmethod
    def tearDownClass(cls):
        return None

    def runTest(self):
        return None

    def countTestCases(self):
        return 1

    def defaultTestResult(self):
        return TestResult()

    def run(self, result=None):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def __call__(self, result=None):
        return self.run(result)

    def debug(self):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def id(self):
        return self._testMethodName

    def shortDescription(self):
        return None

    def _formatMessage(self, msg, standardMsg):
        if not self.longMessage:
            if msg is None:
                return standardMsg
            return str(msg)
        if msg is None:
            return standardMsg
        return standardMsg + " : " + str(msg)

    def fail(self, msg=None):
        message = "" if msg is None else str(msg)
        raise self.failureException(message)

    def skipTest(self, reason):
        raise SkipTest(reason)

    def _fail(self, standard, msg):
        self.fail(self._formatMessage(msg, standard))

    def assertEqual(self, first, second, msg=None):
        if not first == second:
            self._fail(repr(first) + " != " + repr(second), msg)

    def assertNotEqual(self, first, second, msg=None):
        if first == second:
            self._fail(repr(first) + " == " + repr(second), msg)

    def assertTrue(self, expr, msg=None):
        if not expr:
            self._fail(repr(expr) + " is not true", msg)

    def assertFalse(self, expr, msg=None):
        if expr:
            self._fail(repr(expr) + " is not false", msg)

    def assertIs(self, first, second, msg=None):
        if first is not second:
            self._fail(repr(first) + " is not " + repr(second), msg)

    def assertIsNot(self, first, second, msg=None):
        if first is second:
            self._fail("unexpectedly identical: " + repr(first), msg)

    def assertIsNone(self, obj, msg=None):
        if obj is not None:
            self._fail(repr(obj) + " is not None", msg)

    def assertIsNotNone(self, obj, msg=None):
        if obj is None:
            self._fail("unexpectedly None", msg)

    def assertIn(self, member, container, msg=None):
        if member not in container:
            self._fail(
                repr(member) + " not found in " + repr(container), msg
            )

    def assertNotIn(self, member, container, msg=None):
        if member in container:
            self._fail(
                repr(member) + " unexpectedly found in " + repr(container),
                msg,
            )

    def assertIsInstance(self, obj, cls, msg=None):
        if not isinstance(obj, cls):
            self._fail(
                repr(obj) + " is not an instance of " + repr(cls), msg
            )

    def assertNotIsInstance(self, obj, cls, msg=None):
        if isinstance(obj, cls):
            self._fail(
                repr(obj) + " is an instance of " + repr(cls), msg
            )

    def assertGreater(self, first, second, msg=None):
        if not first > second:
            self._fail(
                repr(first) + " not greater than " + repr(second), msg
            )

    def assertGreaterEqual(self, first, second, msg=None):
        if not first >= second:
            self._fail(
                repr(first) + " not greater than or equal to " + repr(second),
                msg,
            )

    def assertLess(self, first, second, msg=None):
        if not first < second:
            self._fail(repr(first) + " not less than " + repr(second), msg)

    def assertLessEqual(self, first, second, msg=None):
        if not first <= second:
            self._fail(
                repr(first) + " not less than or equal to " + repr(second),
                msg,
            )

    def assertAlmostEqual(
        self, first, second, places=None, msg=None, delta=None
    ):
        if first == second:
            return
        if delta is not None and places is not None:
            raise TypeError("specify delta or places not both")
        difference = abs(first - second)
        if delta is not None:
            if difference <= delta:
                return
            self._fail(
                repr(first)
                + " != "
                + repr(second)
                + " within "
                + repr(delta)
                + " delta ("
                + repr(difference)
                + " difference)",
                msg,
            )
            return
        if places is None:
            places = 7
        if round(difference, places) != 0:
            self._fail(
                repr(first)
                + " != "
                + repr(second)
                + " within "
                + repr(places)
                + " places ("
                + repr(difference)
                + " difference)",
                msg,
            )

    def assertNotAlmostEqual(
        self, first, second, places=None, msg=None, delta=None
    ):
        if delta is not None and places is not None:
            raise TypeError("specify delta or places not both")
        difference = abs(first - second)
        if delta is not None:
            if first != second and difference > delta:
                return
            self._fail(
                repr(first)
                + " == "
                + repr(second)
                + " within "
                + repr(delta)
                + " delta ("
                + repr(difference)
                + " difference)",
                msg,
            )
            return
        if places is None:
            places = 7
        if first != second and round(difference, places) != 0:
            return
        self._fail(
            repr(first)
            + " == "
            + repr(second)
            + " within "
            + repr(places)
            + " places",
            msg,
        )

    def assertSequenceEqual(self, seq1, seq2, msg=None, seq_type=None):
        if seq_type is not None:
            if not isinstance(seq1, seq_type) or not isinstance(seq2, seq_type):
                raise TypeError("sequences must have the requested type")
        if seq1 != seq2:
            self._fail(repr(seq1) + " != " + repr(seq2), msg)

    def assertListEqual(self, list1, list2, msg=None):
        if not isinstance(list1, list) or not isinstance(list2, list):
            raise TypeError("assertListEqual requires two lists")
        self.assertSequenceEqual(list1, list2, msg, list)

    def assertTupleEqual(self, tuple1, tuple2, msg=None):
        if not isinstance(tuple1, tuple) or not isinstance(tuple2, tuple):
            raise TypeError("assertTupleEqual requires two tuples")
        self.assertSequenceEqual(tuple1, tuple2, msg, tuple)

    def assertDictEqual(self, first, second, msg=None):
        if not isinstance(first, dict) or not isinstance(second, dict):
            raise TypeError("assertDictEqual requires two dictionaries")
        if first != second:
            self._fail(repr(first) + " != " + repr(second), msg)

    def assertSetEqual(self, first, second, msg=None):
        if first != second:
            self._fail(repr(first) + " != " + repr(second), msg)

    def assertRegex(self, text, regex, msg=None):
        if not isinstance(regex, str):
            raise NotImplementedError(
                "compiled regular expressions in assertRegex are not "
                "runtime-owned"
            )
        if re.search(regex, text) is None:
            self._fail(
                "Regex didn't match: "
                + repr(regex)
                + " not found in "
                + repr(text),
                msg,
            )

    def assertNotRegex(self, text, regex, msg=None):
        if not isinstance(regex, str):
            raise NotImplementedError(
                "compiled regular expressions in assertNotRegex are not "
                "runtime-owned"
            )
        if re.search(regex, text) is not None:
            self._fail(
                "Regex matched: " + repr(regex) + " matches " + repr(text),
                msg,
            )

    def assertRaises(
        self, expected_exception, callable_obj=_MISSING, *args, **kwargs
    ):
        _validate_expected_exception(expected_exception)
        if callable_obj is _MISSING:
            if len(kwargs) > 1 or (kwargs and "msg" not in kwargs):
                raise TypeError("assertRaises context accepts only msg")
            message = kwargs.get("msg")
            return _AssertRaisesContext(
                expected_exception, self, message=message
            )
        context = _AssertRaisesContext(expected_exception, self)
        with context:
            callable_obj(*args, **kwargs)
        return None

    def assertRaisesRegex(
        self,
        expected_exception,
        expected_regex,
        callable_obj=_MISSING,
        *args,
        **kwargs,
    ):
        _validate_expected_exception(expected_exception)
        if not isinstance(expected_regex, str):
            raise NotImplementedError(
                "compiled regular expressions in assertRaisesRegex are not "
                "runtime-owned"
            )
        if callable_obj is _MISSING:
            if len(kwargs) > 1 or (kwargs and "msg" not in kwargs):
                raise TypeError("assertRaisesRegex context accepts only msg")
            message = kwargs.get("msg")
            return _AssertRaisesContext(
                expected_exception,
                self,
                expected_regex,
                message,
            )
        context = _AssertRaisesContext(expected_exception, self, expected_regex)
        with context:
            callable_obj(*args, **kwargs)
        return None

    def subTest(self, msg=_MISSING, **params):
        raise NotImplementedError(
            "unittest subtest result isolation and continuation are not "
            "runtime-owned"
        )

    def addCleanup(self, function, *args, **kwargs):
        raise NotImplementedError(
            "unittest cleanup result integration is not runtime-owned"
        )

    def doCleanups(self):
        raise NotImplementedError(
            "unittest cleanup result integration is not runtime-owned"
        )

    def enterContext(self, cm):
        raise NotImplementedError(
            "unittest cleanup result integration is not runtime-owned"
        )

    def assertWarns(self, expected_warning, callable_obj=_MISSING, *args, **kwargs):
        raise NotImplementedError(
            "unittest warning capture is not runtime-owned"
        )

    def assertWarnsRegex(
        self,
        expected_warning,
        expected_regex,
        callable_obj=_MISSING,
        *args,
        **kwargs,
    ):
        raise NotImplementedError(
            "unittest warning capture is not runtime-owned"
        )

    def assertLogs(self, logger=None, level=None):
        raise NotImplementedError("unittest log capture is not runtime-owned")

    def assertNoLogs(self, logger=None, level=None):
        raise NotImplementedError("unittest log capture is not runtime-owned")


class FunctionTestCase(TestCase):
    def __init__(
        self,
        testFunc,
        setUp=None,
        tearDown=None,
        description=None,
    ):
        TestCase.__init__(self, "runTest")
        self._testFunc = testFunc
        self._setUpFunc = setUp
        self._tearDownFunc = tearDown
        self._description = description

    def runTest(self):
        return self._testFunc()

    def shortDescription(self):
        return self._description


class IsolatedAsyncioTestCase(TestCase):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "IsolatedAsyncioTestCase event-loop lifecycle is not runtime-owned"
        )


class TestResult:
    """Inspectable empty result; mutation is owned only by a real runner."""

    def __init__(self, stream=None, descriptions=None, verbosity=None):
        self.testsRun = 0
        self.failures = []
        self.errors = []
        self.skipped = []
        self.expectedFailures = []
        self.unexpectedSuccesses = []
        self.shouldStop = False

    def wasSuccessful(self):
        return not self.failures and not self.errors and not self.unexpectedSuccesses

    def stop(self):
        self.shouldStop = True

    def startTestRun(self):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def stopTestRun(self):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def startTest(self, test):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def stopTest(self, test):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addError(self, test, err):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addFailure(self, test, err):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addSuccess(self, test):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addSkip(self, test, reason):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addExpectedFailure(self, test, err):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addUnexpectedSuccess(self, test):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def addSubTest(self, test, subtest, err):
        raise NotImplementedError(_RUNNER_BOUNDARY)


class TextTestResult(TestResult):
    pass


class TestSuite:
    """Finite test container; running it still requires result ownership."""

    def __init__(self, tests=()):
        self._tests = []
        if tests is not None:
            self.addTests(tests)

    def __iter__(self):
        return iter(self._tests)

    def addTest(self, test):
        if not callable(test):
            raise TypeError("TestSuite entries must be callable test objects")
        self._tests.append(test)

    def addTests(self, tests):
        for test in tests:
            self.addTest(test)

    def countTestCases(self):
        total = 0
        for test in self._tests:
            total += test.countTestCases()
        return total

    def run(self, result, debug=False):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def __call__(self, result, debug=False):
        return self.run(result, debug)

    def debug(self):
        raise NotImplementedError(_RUNNER_BOUNDARY)


class TestLoader:
    """Configuration-compatible loader whose reflective operations fail."""

    testMethodPrefix = "test"
    sortTestMethodsUsing = None
    suiteClass = TestSuite
    testNamePatterns = None

    def __init__(self):
        self.errors = []

    def loadTestsFromTestCase(self, testCaseClass):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def loadTestsFromModule(self, module, pattern=None):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def loadTestsFromName(self, name, module=None):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def loadTestsFromNames(self, names, module=None):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def getTestCaseNames(self, testCaseClass):
        raise NotImplementedError(_RUNNER_BOUNDARY)

    def discover(
        self, start_dir, pattern="test*.py", top_level_dir=None
    ):
        raise NotImplementedError(_RUNNER_BOUNDARY)


defaultTestLoader = TestLoader()


class TextTestRunner:
    resultclass = TextTestResult

    def __init__(
        self,
        stream=None,
        descriptions=True,
        verbosity=1,
        failfast=False,
        buffer=False,
        resultclass=None,
        warnings=None,
        tb_locals=False,
        durations=None,
    ):
        if resultclass is not None:
            self.resultclass = resultclass
        self.stream = stream
        self.descriptions = descriptions
        self.verbosity = verbosity
        self.failfast = failfast
        self.buffer = buffer

    def run(self, test):
        raise NotImplementedError(_RUNNER_BOUNDARY)


def skip(reason):
    def decorator(test_item):
        if not isinstance(test_item, type):
            @functools.wraps(test_item)
            def skipped(*args, **kwargs):
                raise SkipTest(reason)

            test_item = skipped
        # CPython's marks: a runner skips a marked class or test without
        # calling it.
        test_item.__unittest_skip__ = True
        test_item.__unittest_skip_why__ = reason
        return test_item

    return decorator


def skipIf(condition, reason):
    if condition:
        return skip(reason)

    def identity(test_item):
        return test_item

    return identity


def skipUnless(condition, reason):
    return skipIf(not condition, reason)


def expectedFailure(test_item):
    if isinstance(test_item, type):
        raise NotImplementedError(
            "class-level expectedFailure needs reflective test discovery"
        )

    def unsupported_expected_failure(*args, **kwargs):
        raise NotImplementedError(
            "unittest expected-failure result accounting is not runtime-owned"
        )

    return unsupported_expected_failure


def main(*args, **kwargs):
    raise NotImplementedError(_RUNNER_BOUNDARY)


TestProgram = main


def installHandler():
    raise NotImplementedError("unittest signal-aware result control is not owned")


def registerResult(result):
    raise NotImplementedError("unittest signal-aware result control is not owned")


def removeResult(result):
    raise NotImplementedError("unittest signal-aware result control is not owned")


def removeHandler(method=None):
    raise NotImplementedError("unittest signal-aware result control is not owned")


def addModuleCleanup(function, *args, **kwargs):
    raise NotImplementedError(
        "unittest module cleanup result integration is not runtime-owned"
    )


def doModuleCleanups():
    raise NotImplementedError(
        "unittest module cleanup result integration is not runtime-owned"
    )


def enterModuleContext(cm):
    raise NotImplementedError(
        "unittest module cleanup result integration is not runtime-owned"
    )
