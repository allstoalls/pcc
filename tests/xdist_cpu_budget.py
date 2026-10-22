"""Conservative inner CPU budgets for trusted, local stock-xdist sessions.

Keep the configured width during collection.  Only stock loadgroup's final,
identical collection can prove a smaller, immutable outer concurrency bound.
One-way local files carry that proof before dispatch; there is no live-worker
count, group-name parsing, polling, or change to xdist's scheduling policy.
Unknown hooks, remote launchers and unavailable proof retain the old budget.
This is a local test optimization, not a sandbox for untrusted pytest plugins.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import uuid

import pytest

from scripts.pytest_live_report import LiveReport

_OUTER_ENV = "PCC_OUTER_PARALLELISM"
_PROOF_INPUT = "pcc_local_loadgroup_bound"
_PROOF_SCHEMA = "pcc.local-loadgroup-item-bound.v1"
_AUTO_WIDTH = pytest.StashKey[str]()
_CONSUMED = pytest.StashKey[bool]()
_PROOF_DIR = pytest.StashKey[tempfile.TemporaryDirectory]()
_LIVE_REPORT_COLLECTION_HOOK = LiveReport.pytest_xdist_node_collection_finished


def pytest_configure(config):
    """Publish configured outer width first; preserve every explicit value."""

    if not hasattr(config, "workerinput") or _OUTER_ENV in os.environ:
        return
    raw_count = str(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "") or "").strip()
    try:
        worker_count = max(1, int(raw_count))
    except ValueError:
        worker_count = 1
    value = str(worker_count)
    os.environ[_OUTER_ENV] = value
    config.stash[_AUTO_WIDTH] = value


def _plain_local_workers(node):
    specs = node.nodemanager.specs
    allowed = {"_spec", "env", "execmodel", "popen", "id"}
    return (
        bool(specs)
        and node.gateway.spec in specs
        and all(
            spec.popen is True and not spec.env and set(vars(spec)) <= allowed
            for spec in specs
        )
    )


def _only_hook(config, name, function):
    implementations = getattr(config.hook, name).get_hookimpls()
    return bool(implementations) and all(
        not implementation.hookwrapper
        and not implementation.wrapper
        and getattr(implementation.function, "__func__", implementation.function)
        is function
        for implementation in implementations
    )


def _known_collection_hooks(config):
    found_budget_hook = False
    for (
        implementation
    ) in config.hook.pytest_xdist_node_collection_finished.get_hookimpls():
        if implementation.hookwrapper or implementation.wrapper:
            return False
        function = getattr(implementation.function, "__func__", implementation.function)
        if function is pytest_xdist_node_collection_finished:
            found_budget_hook = True
        elif not (
            # The qualification gate's known observer only records IDs. Do not
            # generalize this exception to subclasses or other observer hooks.
            function is _LIVE_REPORT_COLLECTION_HOOK
            and type(implementation.plugin) is LiveReport
        ):
            return False
    return found_budget_hook


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    """Give each plain local worker a fresh, initially absent proof path."""

    try:
        if (
            _OUTER_ENV in os.environ
            or node.config.getoption("dist", None) != "loadgroup"
            or not _plain_local_workers(node)
        ):
            return
        directory = node.config.stash.get(_PROOF_DIR, None)
        if directory is None:
            directory = tempfile.TemporaryDirectory(prefix="pcc-xdist-budget-")
            node.config.add_cleanup(directory.cleanup)
            node.config.stash[_PROOF_DIR] = directory
        node.workerinput[_PROOF_INPUT] = str(Path(directory.name) / uuid.uuid4().hex)
    except (AttributeError, OSError, TypeError, ValueError):
        return


def _stock_local_scheduler(node):
    from xdist.dsession import DSession
    from xdist.scheduler.loadgroup import LoadGroupScheduling
    from xdist.workermanage import WorkerController

    config = node.config
    session = config.pluginmanager.getplugin("dsession")
    if (
        config.getoption("dist", None) != "loadgroup"
        or not _plain_local_workers(node)
        or type(session) is not DSession
        or type(session.sched) is not LoadGroupScheduling
        or not _only_hook(
            config, "pytest_xdist_make_scheduler", DSession.pytest_xdist_make_scheduler
        )
        or not _known_collection_hooks(config)
        or not _only_hook(
            config,
            "pytest_xdist_getremotemodule",
            WorkerController.RemoteHook.pytest_xdist_getremotemodule,
        )
    ):
        return None
    return session.sched


def _publish_bound(node, item_count):
    raw_path = node.workerinput.get(_PROOF_INPUT)
    if not raw_path or item_count < 1:
        return
    path = Path(raw_path)
    if path.exists():
        return  # A worker's published bound never changes.
    pending = path.with_suffix(".pending")
    with pending.open("x", encoding="ascii") as stream:
        stream.write(f"{_PROOF_SCHEMA}\n{item_count}\n")
    pending.replace(path)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    """Publish only at stock loadgroup's final initial collection barrier."""

    try:
        scheduler = _stock_local_scheduler(node)
        if scheduler is None or not ids:
            return
        if scheduler.collection is not None:
            # Replacement workers must collect the same fixed session universe.
            if list(ids) == scheduler.collection:
                _publish_bound(node, len(ids))
            return
        collections = dict(scheduler.registered_collections)
        collections[node] = list(ids)
        if (
            len(collections) != scheduler.numnodes
            or set(collections) != set(scheduler.nodes)
            or any(collection != list(ids) for collection in collections.values())
        ):
            return
        # The stock callback adds this last collection and dispatches only after
        # this hook returns. No worker can enter setup before its proof is ready.
        for worker in scheduler.nodes:
            _publish_bound(worker, len(ids))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Consume a final bound once, immediately before test/fixture execution."""

    config = item.config
    if config.stash.get(_CONSUMED, False):
        return
    config.stash[_CONSUMED] = True
    automatic = config.stash.get(_AUTO_WIDTH, None)
    if automatic is None or os.environ.get(_OUTER_ENV) != automatic:
        return
    raw_path = config.workerinput.get(_PROOF_INPUT)
    if not raw_path:
        return
    try:
        with open(raw_path, encoding="ascii") as stream:
            proof = stream.read(128)
        schema, raw_count, end = proof.split("\n")
        item_count = int(raw_count)
        if schema != _PROOF_SCHEMA or end or item_count < 1:
            return
    except (OSError, TypeError, ValueError, UnicodeError):
        return
    os.environ[_OUTER_ENV] = str(min(int(automatic), item_count))
