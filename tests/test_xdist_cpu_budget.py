"""Execute the outer-width policy without compiling or provisioning anything."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

from execnet import XSpec
import pytest
from xdist.dsession import DSession
from xdist.scheduler.loadgroup import LoadGroupScheduling
from xdist.workermanage import WorkerController

from tests import xdist_cpu_budget as budget

ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_WORKFLOW = ROOT / ".github/workflows/pcc1-package-parity.yml"
_PACKAGE_BUDGET_INPUTS = (
    "tests/conftest.py",
    "tests/xdist_cpu_budget.py",
    "tests/test_xdist_cpu_budget.py",
    "tests/test_test_infrastructure_efficiency.py",
)
# Nested six-worker probes run sequentially within the normal six-worker suite.
pytestmark = pytest.mark.xdist_group("pcc_xdist_budget_contract")


def _mapping_block(lines, key, indent):
    """Read this workflow's literal block mappings without a YAML dependency."""

    marker = " " * indent + key + ":"
    starts = [index for index, line in enumerate(lines) if line == marker]
    assert len(starts) == 1, (key, starts)
    block = []
    for line in lines[starts[0] + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return block


def _package_event_paths(source, event):
    events = _mapping_block(source.splitlines(), "on", 0)
    trigger = _mapping_block(events, event, 2)
    lines = _mapping_block(trigger, "paths", 4)
    prefix = "      - "
    assert all(line.startswith(prefix) for line in lines)
    # The workflow uses quoted literal strings. Exact entries are important:
    # GitHub's path filters match a whole path from the repository root.
    return tuple(json.loads(line[len(prefix) :]) for line in lines)


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("path", _PACKAGE_BUDGET_INPUTS)
def test_package_workflow_tracks_each_xdist_budget_input(event, path):
    patterns = _package_event_paths(
        _PACKAGE_WORKFLOW.read_text(encoding="utf-8"), event
    )
    assert patterns.count(path) == 1
    assert patterns.count("conftest.py") == 1


@pytest.mark.parametrize("path", _PACKAGE_BUDGET_INPUTS)
@pytest.mark.parametrize("lookalike", ["root", "prefix", "suffix"])
def test_package_paths_do_not_accept_other_scopes_or_similar_names(path, lookalike):
    decoy = {
        "root": Path(path).name,
        "prefix": "other/" + path,
        "suffix": path + ".bak",
    }[lookalike]
    source = textwrap.dedent(f"""
        on:
          push:
            paths:
              # {path}
              - "{decoy}"
          pull_request:
            paths:
              - "{path}"
        jobs:
          example:
            steps:
              - run: echo "{path}"
    """)
    push_patterns = _package_event_paths(source, "push")
    assert push_patterns == (decoy,)
    assert path not in push_patterns
    assert _package_event_paths(source, "pull_request") == (path,)


def _implementation(function, **overrides):
    return SimpleNamespace(
        **{"function": function, "hookwrapper": False, "wrapper": False, **overrides}
    )


class _Node:
    def __init__(self, config, specs, index):
        self.config = config
        self.nodemanager = SimpleNamespace(specs=specs)
        self.gateway = SimpleNamespace(id=f"gw{index}", spec=specs[index % len(specs)])
        self.workerinput = {"workerid": f"gw{index}", "workercount": len(specs)}
        self.dispatched = []
        self.shutting_down = False

    def send_runtest_some(self, indices):
        self.dispatched.extend(indices)

    def shutdown(self):
        self.shutting_down = True


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.delenv(budget._OUTER_ENV, raising=False)
    cleanups = []
    config = SimpleNamespace(
        stash={},
        getoption=lambda name, default=None: "loadgroup" if name == "dist" else default,
        getvalue=lambda name: ["popen"] * 6,
        option=SimpleNamespace(loadscopereorder=True),
        add_cleanup=cleanups.append,
    )
    session = object.__new__(DSession)
    scheduler = session.sched = LoadGroupScheduling(config)
    implementations = {
        "pytest_xdist_make_scheduler": [
            _implementation(session.pytest_xdist_make_scheduler)
        ],
        "pytest_xdist_node_collection_finished": [
            _implementation(budget.pytest_xdist_node_collection_finished)
        ],
        "pytest_xdist_getremotemodule": [
            _implementation(WorkerController.RemoteHook().pytest_xdist_getremotemodule)
        ],
    }
    config.hook = SimpleNamespace(
        **{
            name: SimpleNamespace(get_hookimpls=lambda entries=entries: entries)
            for name, entries in implementations.items()
        },
        pytest_collectreport=lambda report: None,
    )
    config.pluginmanager = SimpleNamespace(getplugin=lambda name: session)
    specs = [XSpec("popen") for _ in range(6)]
    nodes = [_Node(config, specs, index) for index in range(6)]
    for node in nodes:
        scheduler.add_node(node)
        budget.pytest_configure_node(node)
    yield SimpleNamespace(
        config=config,
        scheduler=scheduler,
        nodes=nodes,
        implementations=implementations,
        session=session,
    )
    for cleanup in reversed(cleanups):
        cleanup()


def _proof_exists(node):
    path = node.workerinput.get(budget._PROOF_INPUT)
    return bool(path and Path(path).exists())


def _collect(controller, ids):
    for node in controller.nodes:
        budget.pytest_xdist_node_collection_finished(node, ids)
        controller.scheduler.add_node_collection(node, ids)


@pytest.mark.parametrize("item_count", [0, 1, 2, 6, 9])
def test_final_item_bound_is_published_only_at_collection_barrier(
    controller, item_count
):
    ids = [f"test_{index}" for index in range(item_count)]
    for node in controller.nodes[:-1]:
        budget.pytest_xdist_node_collection_finished(node, ids)
        controller.scheduler.add_node_collection(node, ids)
        assert not any(_proof_exists(worker) for worker in controller.nodes)
    last = controller.nodes[-1]
    budget.pytest_xdist_node_collection_finished(last, ids)
    assert all(_proof_exists(node) == bool(ids) for node in controller.nodes)
    controller.scheduler.add_node_collection(last, ids)
    controller.scheduler.schedule()
    active = sum(bool(node.dispatched) for node in controller.nodes)
    assert active <= min(6, item_count)
    assert sorted(i for node in controller.nodes for i in node.dispatched) == list(
        range(item_count)
    )


def test_mismatched_final_collection_never_publishes(controller):
    for index, node in enumerate(controller.nodes):
        ids = ["other"] if index == 5 else ["test_a"]
        budget.pytest_xdist_node_collection_finished(node, ids)
        controller.scheduler.add_node_collection(node, ids)
    assert not any(_proof_exists(node) for node in controller.nodes)
    controller.scheduler.schedule()
    assert not any(node.dispatched for node in controller.nodes)


@pytest.mark.parametrize(
    "hook,flags",
    [
        ("pytest_xdist_make_scheduler", {}),
        ("pytest_xdist_make_scheduler", {"wrapper": True}),
        ("pytest_xdist_make_scheduler", {"hookwrapper": True}),
        ("pytest_xdist_node_collection_finished", {}),
        ("pytest_xdist_getremotemodule", {}),
    ],
)
def test_late_unknown_hooks_cannot_leave_early_proofs(controller, hook, flags):
    for node in controller.nodes[:-1]:
        budget.pytest_xdist_node_collection_finished(node, ["test_a"])
        controller.scheduler.add_node_collection(node, ["test_a"])
    controller.implementations[hook].append(_implementation(lambda: None, **flags))
    budget.pytest_xdist_node_collection_finished(controller.nodes[-1], ["test_a"])
    assert not any(_proof_exists(node) for node in controller.nodes)


def test_scheduler_subclass_is_not_stock(controller):
    class CustomScheduler(LoadGroupScheduling):
        pass

    controller.session.sched = CustomScheduler(controller.config)
    _collect(controller, ["test_a"])
    assert not any(_proof_exists(node) for node in controller.nodes)


@pytest.mark.parametrize("subclass", [False, True])
def test_only_exact_known_live_report_observer_is_allowed(controller, subclass):
    from scripts.pytest_live_report import LiveReport

    class CustomReport(LiveReport):
        pass

    observer = object.__new__(CustomReport if subclass else LiveReport)
    controller.implementations["pytest_xdist_node_collection_finished"].append(
        _implementation(observer.pytest_xdist_node_collection_finished, plugin=observer)
    )
    _collect(controller, ["test_a"])
    assert all(_proof_exists(node) is not subclass for node in controller.nodes)


def test_replaced_live_report_hook_is_not_a_known_observer(controller, monkeypatch):
    from scripts.pytest_live_report import LiveReport

    monkeypatch.setattr(
        LiveReport,
        "pytest_xdist_node_collection_finished",
        lambda self, node, ids: None,
    )
    observer = object.__new__(LiveReport)
    controller.implementations["pytest_xdist_node_collection_finished"].append(
        _implementation(observer.pytest_xdist_node_collection_finished, plugin=observer)
    )
    _collect(controller, ["test_a"])
    assert not any(_proof_exists(node) for node in controller.nodes)


@pytest.mark.parametrize(
    "spec",
    [
        "ssh=host",
        "popen//via=proxy",
        "popen//python=custom-launcher",
        "popen//chdir=somewhere",
        "popen//env:EXAMPLE=value",
    ],
)
def test_remote_or_unverified_launcher_gets_no_proof(controller, spec):
    node = _Node(controller.config, [XSpec(spec)], 0)
    budget.pytest_configure_node(node)
    assert budget._PROOF_INPUT not in node.workerinput


@pytest.mark.parametrize("same_collection", [True, False])
def test_replacement_requires_unchanged_collection(controller, same_collection):
    ids = ["test_a", "test_b"]
    _collect(controller, ids)
    controller.scheduler.schedule()
    replacement = _Node(controller.config, controller.nodes[0].nodemanager.specs, 6)
    budget.pytest_configure_node(replacement)
    assert replacement.workerinput[budget._PROOF_INPUT] not in {
        node.workerinput[budget._PROOF_INPUT] for node in controller.nodes
    }
    controller.scheduler.add_node(replacement)
    budget.pytest_xdist_node_collection_finished(
        replacement, ids if same_collection else ["changed"]
    )
    assert _proof_exists(replacement) is same_collection


def _worker(monkeypatch, path):
    monkeypatch.delenv(budget._OUTER_ENV, raising=False)
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "6")
    config = SimpleNamespace(stash={}, workerinput={budget._PROOF_INPUT: str(path)})
    budget.pytest_configure(config)
    return SimpleNamespace(config=config)


@pytest.mark.parametrize("explicit", ["", "1", "6", "garbage", "-1"])
def test_explicit_environment_is_never_reinterpreted(monkeypatch, tmp_path, explicit):
    monkeypatch.setenv(budget._OUTER_ENV, explicit)
    config = SimpleNamespace(stash={}, workerinput={})
    budget.pytest_configure(config)
    budget.pytest_runtest_setup(SimpleNamespace(config=config))
    assert os.environ[budget._OUTER_ENV] == explicit
    assert budget._AUTO_WIDTH not in config.stash


@pytest.mark.parametrize(
    "item_count,expected", [(1, "1"), (2, "2"), (6, "6"), (9, "6")]
)
def test_worker_consumes_immutable_item_bound(
    monkeypatch, tmp_path, item_count, expected
):
    path = tmp_path / "proof"
    item = _worker(monkeypatch, path)
    path.write_text(f"{budget._PROOF_SCHEMA}\n{item_count}\n", encoding="ascii")
    assert os.environ[budget._OUTER_ENV] == "6"
    budget.pytest_runtest_setup(item)
    assert os.environ[budget._OUTER_ENV] == expected
    path.write_text(f"{budget._PROOF_SCHEMA}\n1\n", encoding="ascii")
    budget.pytest_runtest_setup(item)
    assert os.environ[budget._OUTER_ENV] == expected


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "",
        "1",
        "wrong\n1\n",
        "pcc.local-loadgroup-item-bound.v1\n0\n",
        "pcc.local-loadgroup-item-bound.v1\n-1\n",
        "pcc.local-loadgroup-item-bound.v1\n1\nextra",
    ],
)
def test_missing_or_bad_proof_falls_back_once(monkeypatch, tmp_path, contents):
    path = tmp_path / "proof"
    item = _worker(monkeypatch, path)
    if contents is not None:
        path.write_text(contents, encoding="ascii")
    budget.pytest_runtest_setup(item)
    assert os.environ[budget._OUTER_ENV] == "6"
    path.write_text(f"{budget._PROOF_SCHEMA}\n1\n", encoding="ascii")
    budget.pytest_runtest_setup(item)
    assert os.environ[budget._OUTER_ENV] == "6"


def test_intervening_environment_override_is_preserved(monkeypatch, tmp_path):
    path = tmp_path / "proof"
    item = _worker(monkeypatch, path)
    path.write_text(f"{budget._PROOF_SCHEMA}\n1\n", encoding="ascii")
    monkeypatch.setenv(budget._OUTER_ENV, "4")
    budget.pytest_runtest_setup(item)
    assert os.environ[budget._OUTER_ENV] == "4"


def test_controller_io_failure_keeps_configured_budget(controller, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("unavailable proof directory")

    monkeypatch.setattr(Path, "open", unavailable)
    _collect(controller, ["test_a"])
    assert not any(_proof_exists(node) for node in controller.nodes)


def _custom_scheduler_plugin(kind):
    if kind == "collection_hook":
        return "def pytest_xdist_node_collection_finished(node, ids):\n    pass\n"
    if kind == "late_wrapper":
        return textwrap.dedent("""
            import pytest
            class Wrapper:
                @pytest.hookimpl(wrapper=True)
                def pytest_xdist_make_scheduler(self, config, log):
                    return (yield)
            @pytest.hookimpl(tryfirst=True)
            def pytest_runtestloop(session):
                if not hasattr(session.config, 'workerinput'):
                    session.config.pluginmanager.register(Wrapper(), 'late-scheduler')
        """)
    if kind == "remote_wrapper":
        return textwrap.dedent("""
            import pytest
            @pytest.hookimpl(wrapper=True)
            def pytest_xdist_getremotemodule():
                return (yield)
        """)
    scheduler = "Subclass" if kind == "subclass" else "LoadGroupScheduling"
    return textwrap.dedent(f"""
        from xdist.scheduler.loadgroup import LoadGroupScheduling
        class Subclass(LoadGroupScheduling):
            pass
        def pytest_xdist_make_scheduler(config, log):
            return {scheduler}(config, log)
    """)


@pytest.mark.parametrize(
    "case,item_count,expected",
    [
        ("single", 1, "1"),
        ("live_report", 1, "1"),
        ("restart_before_collection", 1, "1"),
        ("two", 2, "2"),
        ("many", 7, "6"),
        ("same_group", 2, "2"),
        ("deselected", 2, "1"),
        ("each", 1, "6"),
        ("explicit", 1, "3"),
        ("empty_explicit", 1, ""),
        ("invalid_explicit", 1, "invalid"),
        ("custom", 1, "6"),
        ("subclass", 1, "6"),
        ("late_wrapper", 1, "6"),
        ("collection_hook", 1, "6"),
        ("remote_wrapper", 1, "6"),
    ],
)
def test_real_xdist_budget_contract(tmp_path, case, item_count, expected):
    """Each inner run executes tiny probes with all six real xdist workers."""

    records = tmp_path / "records"
    records.mkdir()
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    probe = tmp_path / "test_probe.py"
    source = textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import pytest
        from pcc.frontends.python import pipeline_pass_config
        from tests import xdist_cpu_budget
        COLLECTION_OUTER = os.environ.get('PCC_OUTER_PARALLELISM')
        def record(request):
            pipeline_pass_config.os.cpu_count = lambda: 12
            value = {
                'width': os.environ.get('PCC_OUTER_PARALLELISM'),
                'collection_width': COLLECTION_OUTER,
                'configured': os.environ.get('PYTEST_XDIST_WORKER_COUNT'),
                'cpu_budget': pipeline_pass_config.parallel_cpu_budget(),
                'proof': request.config.workerinput.get(xdist_cpu_budget._PROOF_INPUT),
            }
            path = Path(os.environ['PCC_BUDGET_RECORDS']) / (
                os.environ['PYTEST_XDIST_WORKER'] + '_' + request.node.name + '.json'
            )
            path.write_text(json.dumps(value), encoding='utf-8')
    """)
    for index in range(item_count):
        if case == "same_group":
            source += "\n@pytest.mark.xdist_group('one_group')\n"
        source += f"\ndef test_probe_{index}(request):\n    record(request)\n"
    probe.write_text(source, encoding="utf-8")
    if case in {
        "custom",
        "subclass",
        "late_wrapper",
        "collection_hook",
        "remote_wrapper",
    }:
        (tmp_path / "conftest.py").write_text(
            _custom_scheduler_plugin(case), encoding="utf-8"
        )
    if case == "restart_before_collection":
        (tmp_path / "conftest.py").write_text(
            textwrap.dedent("""
                import os
                import pytest
                @pytest.hookimpl(tryfirst=True)
                def pytest_sessionstart(session):
                    if getattr(session.config, 'workerinput', {}).get('workerid') == 'gw0':
                        os._exit(7)
            """),
            encoding="utf-8",
        )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop(budget._OUTER_ENV, None)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONPATH"] = str(ROOT)
    env["PCC_BUDGET_RECORDS"] = str(records)
    if case in {"explicit", "empty_explicit", "invalid_explicit"}:
        env[budget._OUTER_ENV] = expected
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "-q",
        "-n",
        "6",
        "--dist=" + ("each" if case == "each" else "loadgroup"),
        "-c",
        str(config),
        "--confcutdir",
        str(tmp_path),
        "-p",
        "tests.conftest",
        str(probe),
    ]
    if case == "deselected":
        command += ["-k", "test_probe_0"]
    if case == "live_report":
        command += [
            "-p",
            "scripts.pytest_live_report",
            "--pcc-live-report",
            str(tmp_path / "live.jsonl"),
        ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if case == "restart_before_collection":
        assert "[gw0] node down: Not properly terminated" in result.stdout
        assert result.stdout.count("bringing up nodes...") == 2
    observed = [
        json.loads(path.read_text(encoding="utf-8")) for path in records.glob("*.json")
    ]
    expected_records = (
        6 if case == "each" else 1 if case == "deselected" else item_count
    )
    assert len(observed) == expected_records, result.stdout + result.stderr
    assert {record["configured"] for record in observed} == {"6"}
    assert {record["width"] for record in observed} == {expected}
    collection_width = expected if "explicit" in case else "6"
    assert {record["collection_width"] for record in observed} == {collection_width}
    try:
        divisor = max(1, int(expected))
    except ValueError:
        divisor = 1
    assert {record["cpu_budget"] for record in observed} == {max(1, 12 // divisor)}
    assert all(
        not Path(record["proof"]).exists() for record in observed if record["proof"]
    )
    if case == "live_report":
        reports = [
            json.loads(line)
            for line in (tmp_path / "live.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [
            len(report["nodeids"])
            for report in reports
            if report["event"] == "collected"
        ] == [1]
        assert reports[-1]["event"] == "finish"
        assert reports[-1]["exitstatus"] == 0
