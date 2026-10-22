"""Per-case runner for the CPython ``Lib/test`` conformance harness.

The harness installs this file next to each generated driver as
``_pcc_conformance_runner``. Under CPython, ``run_module`` collects the
cases as unittest's default loader does -- ``dir(module)`` in sorted order,
every ``unittest.TestCase`` subclass found there and its callable
``test*`` attributes in sorted order (``load_tests`` hooks are not
consulted) -- prints that plan and runs it: the baseline. The native
driver receives the same plan and ``run_plan`` replays it with plain
attribute lookups, so both sides run the same cases under the same ids and
case discovery is not part of what is measured. Cases are driven here
instead of through ``unittest.main()`` or a ``TestResult``. Each case follows CPython 3.15's
``TestCase.run``: a skipped class or method is skipped; ``setUp`` runs, then
the method and ``tearDown`` only if ``setUp`` succeeded, and the cleanups
always; ``SkipTest`` skips, the failure exception (``AssertionError``) is a
failure, anything else an error; with ``expectedFailure`` an exception in
the method is an expected failure and its absence an unexpected success.
``setUpModule``/``setUpClass`` failures mark the cases they guard.

Every result is one flushed line, so a crash keeps the cases before it::

    @@PCC-CONFORMANCE@@ CLASS <attribute> <module>.<Class> <method> ...
    @@PCC-CONFORMANCE@@ CASE <STATUS> <module>.<Class>.<method>[ :: <detail>]

(CLASS lines, the plan, come from ``run_module`` only.)

STATUS is PASS, FAIL, ERROR, SKIP, XFAIL or UPASS; PASS and XFAIL are
successes, as in ``TestResult.wasSuccessful``. The last line is
``@@PCC-CONFORMANCE@@ DONE <count>``.
"""

import sys
import unittest


MARK = "@@PCC-CONFORMANCE@@"


def _one_line(text):
    text = str(text).replace("\r", " ").replace("\n", " ")
    if len(text) > 300:
        text = text[:300] + "..."
    return text


def _describe(error):
    try:
        message = str(error)
    except BaseException:
        message = "<unprintable>"
    return _one_line(type(error).__name__ + ": " + message)


def _emit(status, case_id, detail):
    line = MARK + " CASE " + status + " " + case_id
    if detail:
        line = line + " :: " + _one_line(detail)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _class_id(cls):
    return str(cls.__module__) + "." + str(cls.__qualname__)


def _test_classes(module):
    """(attribute name, class) of every TestCase subclass, loader order."""
    classes = []
    for name in sorted(dir(module)):
        value = getattr(module, name)
        if isinstance(value, type) and issubclass(value, unittest.TestCase):
            classes.append((name, value))
    return classes


def _test_names(cls):
    names = []
    for name in sorted(dir(cls)):
        if name.startswith("test") and callable(getattr(cls, name, None)):
            names.append(name)
    return names


class _Outcome:
    """The accumulated outcome of one case's parts."""

    def __init__(self):
        self.status = ""
        self.detail = ""
        self.expected_failure = ""

    def record(self, status, detail):
        # An error outranks a failure, which outranks a skip.
        rank = {"SKIP": 1, "FAIL": 2, "ERROR": 3}
        if rank[status] > rank.get(self.status, 0):
            self.status = status
            self.detail = detail

    def part(self, function, expecting_failure):
        """Run one part; True when it raised nothing."""
        try:
            function()
        except KeyboardInterrupt:
            raise
        except unittest.SkipTest as error:
            self.record("SKIP", str(error))
            return False
        except BaseException as error:
            if expecting_failure:
                self.expected_failure = _describe(error)
                return True
            if isinstance(error, AssertionError):
                self.record("FAIL", _describe(error))
            else:
                self.record("ERROR", _describe(error))
            return False
        return True


def _run_case(cls, class_id, name):
    case_id = class_id + "." + name
    try:
        case = cls(name)
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        _emit("ERROR", case_id, "instantiation " + _describe(error))
        return
    method = getattr(case, name)
    if getattr(cls, "__unittest_skip__", False) or getattr(method, "__unittest_skip__", False):
        why = getattr(cls, "__unittest_skip_why__", "") or getattr(method, "__unittest_skip_why__", "")
        _emit("SKIP", case_id, why)
        return
    expecting_failure = bool(
        getattr(case, "__unittest_expecting_failure__", False)
        or getattr(method, "__unittest_expecting_failure__", False)
    )
    outcome = _Outcome()
    if outcome.part(case.setUp, False):
        outcome.part(method, expecting_failure)
        outcome.part(case.tearDown, False)
    cleanups_ok = _cleanups(case, outcome)
    if outcome.status:
        _emit(outcome.status, case_id, outcome.detail)
    elif not cleanups_ok:
        _emit("ERROR", case_id, "a cleanup function raised")
    elif expecting_failure and outcome.expected_failure:
        _emit("XFAIL", case_id, outcome.expected_failure)
    elif expecting_failure:
        _emit("UPASS", case_id, "")
    else:
        _emit("PASS", case_id, "")


def _cleanups(case, outcome):
    """Run the case's registered cleanups; False when one of them raised.

    Only a case that registered cleanups calls ``doCleanups``. Outside
    ``TestCase.run`` CPython's ``doCleanups`` swallows a cleanup's exception
    and returns False, which is checked here.
    """
    if not getattr(case, "_cleanups", None):
        return True
    try:
        succeeded = case.doCleanups()
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        outcome.record("ERROR", "doCleanups " + _describe(error))
        return False
    return succeeded is not False


def _mark_all(class_id, names, status, detail):
    for name in names:
        _emit(status, class_id + "." + name, detail)


def _run_class(cls, class_id, names):
    if not names:
        return 0
    if getattr(cls, "__unittest_skip__", False):
        _mark_all(class_id, names, "SKIP", getattr(cls, "__unittest_skip_why__", ""))
        return len(names)
    try:
        cls.setUpClass()
    except KeyboardInterrupt:
        raise
    except unittest.SkipTest as error:
        _mark_all(class_id, names, "SKIP", "setUpClass " + str(error))
        _class_cleanups(cls, class_id)
        return len(names)
    except BaseException as error:
        _mark_all(class_id, names, "ERROR", "setUpClass " + _describe(error))
        _class_cleanups(cls, class_id)
        return len(names)
    for name in names:
        _run_case(cls, class_id, name)
    try:
        cls.tearDownClass()
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        _emit("ERROR", class_id + ".tearDownClass", _describe(error))
    _class_cleanups(cls, class_id)
    return len(names)


def _class_cleanups(cls, class_id):
    cleanups = getattr(cls, "doClassCleanups", None)
    if cleanups is None:
        return
    try:
        cleanups()
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        _emit("ERROR", class_id + ".doClassCleanups", _describe(error))


def run_module(module):
    """Collect the cases of ``module``, print the plan and run it."""
    plan = []
    for name, cls in _test_classes(module):
        class_id = _class_id(cls)
        names = _test_names(cls)
        sys.stdout.write(MARK + " CLASS " + name + " " + class_id + " " + " ".join(names) + "\n")
        plan.append((name, class_id, names))
    sys.stdout.flush()
    return run_plan(module, plan)


def run_plan(module, plan):
    """Run ``plan`` -- (attribute, class id, method names) per class -- of
    ``module`` and print one line per case."""
    count = 0
    set_up = getattr(module, "setUpModule", None)
    module_failure = ""
    module_status = ""
    if set_up is not None:
        try:
            set_up()
        except KeyboardInterrupt:
            raise
        except unittest.SkipTest as error:
            module_status = "SKIP"
            module_failure = "setUpModule " + str(error)
        except BaseException as error:
            module_status = "ERROR"
            module_failure = "setUpModule " + _describe(error)
    for entry in plan:
        name = entry[0]
        class_id = entry[1]
        names = entry[2]
        if module_status:
            _mark_all(class_id, names, module_status, module_failure)
            count = count + len(names)
            continue
        try:
            cls = getattr(module, name)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            _mark_all(class_id, names, "ERROR", "class lookup " + _describe(error))
            count = count + len(names)
            continue
        count = count + _run_class(cls, class_id, names)
    if not module_status:
        tear_down = getattr(module, "tearDownModule", None)
        if tear_down is not None:
            try:
                tear_down()
            except KeyboardInterrupt:
                raise
            except BaseException as error:
                _emit("ERROR", str(module.__name__) + ".tearDownModule", _describe(error))
    # Only module cleanups that were registered (unittest.case keeps them)
    # are run, as with the per-case cleanups.
    case_module = getattr(unittest, "case", None)
    module_cleanups = None
    if case_module is not None and getattr(case_module, "_module_cleanups", None):
        module_cleanups = getattr(unittest, "doModuleCleanups", None)
    if module_cleanups is not None:
        try:
            module_cleanups()
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            _emit("ERROR", str(module.__name__) + ".doModuleCleanups", _describe(error))
    sys.stdout.write(MARK + " DONE " + str(count) + "\n")
    sys.stdout.flush()
    return count
