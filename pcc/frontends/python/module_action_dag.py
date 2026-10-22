"""Deterministic per-module action identities and invalidation planning.

This module owns only compiler action metadata.  It does not read packages,
run workers, or decide which Python semantics are public.  The frontend export
phase supplies the finite public facts; this owner validates and hashes them,
then computes the smallest rebuild closure that is safe to reuse.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import tempfile
from typing import Any, Iterable, Optional


SCHEMA = "pcc.python-module-action-dag.v1"
ACTION_STAGES = (
    "source-discovery",
    "parse-type-export",
    "module-ir",
    "transforms",
    "object-emission",
)
_DEPENDENT_STAGES = (
    "module-ir",
    "transforms",
    "object-emission",
)


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_digest(path: str) -> str:
    """Hash one module source without depending on path or mtime identity."""

    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _is_digest(value: str) -> bool:
    if len(value) != 64:
        return False
    for char in value:
        if char not in "0123456789abcdef":
            return False
    return True


def _canonical_strings(values: Iterable[str], field: str) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw)
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("invalid " + field + " fact")
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(sorted(out))


@dataclass(frozen=True)
class PublicSummary:
    imported_types: tuple[str, ...]
    exports: tuple[str, ...]
    effects: tuple[str, ...]
    layouts: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        imported_types: Iterable[str] = (),
        exports: Iterable[str] = (),
        effects: Iterable[str] = (),
        layouts: Iterable[str] = (),
    ) -> "PublicSummary":
        return cls(
            imported_types=_canonical_strings(imported_types, "imported-type"),
            exports=_canonical_strings(exports, "export"),
            effects=_canonical_strings(effects, "effect"),
            layouts=_canonical_strings(layouts, "layout"),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "effects": list(self.effects),
            "exports": list(self.exports),
            "imported_types": list(self.imported_types),
            "layouts": list(self.layouts),
        }

    def digest(self) -> str:
        return _sha256_json(self.payload())


@dataclass(frozen=True)
class ModuleState:
    name: str
    source_digest: str
    dependencies: tuple[str, ...]
    summary: PublicSummary

    @classmethod
    def create(
        cls,
        name: str,
        source_digest: str,
        dependencies: Iterable[str],
        summary: PublicSummary,
    ) -> "ModuleState":
        clean_name = str(name)
        if not clean_name or "\x00" in clean_name or "\n" in clean_name:
            raise ValueError("invalid module name")
        if not _is_digest(str(source_digest)):
            raise ValueError("invalid source digest for " + clean_name)
        return cls(
            name=clean_name,
            source_digest=str(source_digest),
            dependencies=_canonical_strings(dependencies, "dependency"),
            summary=summary,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "dependencies": list(self.dependencies),
            "name": self.name,
            "source_digest": self.source_digest,
            "summary": self.summary.payload(),
            "summary_digest": self.summary.digest(),
        }


@dataclass(frozen=True)
class GraphState:
    compiler_digest: str
    runtime_abi_digest: str
    target: str
    options_digest: str
    modules: tuple[ModuleState, ...]

    @classmethod
    def create(
        cls,
        *,
        compiler_digest: str,
        runtime_abi_digest: str,
        target: str,
        options_digest: str,
        modules: Iterable[ModuleState],
    ) -> "GraphState":
        digests = (
            ("compiler", compiler_digest),
            ("runtime ABI", runtime_abi_digest),
            ("options", options_digest),
        )
        for label, digest in digests:
            if not _is_digest(str(digest)):
                raise ValueError("invalid " + label + " digest")
        clean_target = str(target)
        if not clean_target or "\x00" in clean_target or "\n" in clean_target:
            raise ValueError("invalid target")
        ordered = sorted(modules, key=lambda item: item.name)
        seen: set[str] = set()
        for module in ordered:
            if module.name in seen:
                raise ValueError("duplicate module " + module.name)
            seen.add(module.name)
        for module in ordered:
            for dependency in module.dependencies:
                if dependency not in seen:
                    raise ValueError(
                        "unknown dependency " + dependency + " from " + module.name
                    )
        return cls(
            compiler_digest=str(compiler_digest),
            runtime_abi_digest=str(runtime_abi_digest),
            target=clean_target,
            options_digest=str(options_digest),
            modules=tuple(ordered),
        )

    def module_map(self) -> dict[str, ModuleState]:
        return {module.name: module for module in self.modules}

    def payload(self) -> dict[str, Any]:
        return {
            "compiler_digest": self.compiler_digest,
            "modules": [module.payload() for module in self.modules],
            "options_digest": self.options_digest,
            "runtime_abi_digest": self.runtime_abi_digest,
            "schema": SCHEMA,
            "target": self.target,
        }

    def digest(self) -> str:
        return _sha256_json(self.payload())


@dataclass(frozen=True)
class Action:
    module: str
    stage: str
    key: str
    reason: str


@dataclass(frozen=True)
class ActionPlan:
    actions: tuple[Action, ...]
    full_rebuild: bool
    reason: str

    def modules(self) -> tuple[str, ...]:
        return tuple(sorted({action.module for action in self.actions}))


def _global_identity(state: GraphState) -> tuple[str, str, str, str]:
    return (
        state.compiler_digest,
        state.runtime_abi_digest,
        state.target,
        state.options_digest,
    )


def _reverse_edges(states: Iterable[GraphState]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {}
    for state in states:
        for module in state.modules:
            reverse.setdefault(module.name, set())
        for module in state.modules:
            for dependency in module.dependencies:
                reverse.setdefault(dependency, set()).add(module.name)
    return reverse


def _reverse_closure(
    roots: Iterable[str],
    reverse: dict[str, set[str]],
) -> set[str]:
    closure: set[str] = set(roots)
    pending = sorted(closure, reverse=True)
    while pending:
        current = pending.pop()
        for dependent in sorted(reverse.get(current, ())):
            if dependent in closure:
                continue
            closure.add(dependent)
            pending.append(dependent)
    return closure


def _dependency_summary_closure(
    modules: dict[str, ModuleState],
    module: ModuleState,
) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    seen: set[str] = set()
    pending = list(reversed(module.dependencies))
    while pending:
        dependency = pending.pop()
        if dependency in seen:
            continue
        seen.add(dependency)
        dependency_state = modules[dependency]
        facts.append((dependency, dependency_state.summary.digest()))
        for child in reversed(dependency_state.dependencies):
            if child not in seen:
                pending.append(child)
    return sorted(facts)


def action_key(state: GraphState, module_name: str, stage: str) -> str:
    if stage not in ACTION_STAGES:
        raise ValueError("unknown action stage " + stage)
    modules = state.module_map()
    module = modules.get(module_name)
    if module is None:
        raise KeyError(module_name)
    dependency_summaries = _dependency_summary_closure(modules, module)
    material = {
        "compiler_digest": state.compiler_digest,
        "dependency_summaries": dependency_summaries,
        "module": module.name,
        "options_digest": state.options_digest,
        "runtime_abi_digest": state.runtime_abi_digest,
        "schema": SCHEMA,
        "source_digest": module.source_digest,
        "stage": stage,
        "summary_digest": module.summary.digest(),
        "target": state.target,
    }
    return _sha256_json(material)


def _actions_for(
    state: GraphState,
    modules: Iterable[str],
    stages: Iterable[str],
    reason: str,
) -> list[Action]:
    actions: list[Action] = []
    for module in sorted(modules):
        for stage in ACTION_STAGES:
            if stage not in stages:
                continue
            actions.append(
                Action(
                    module=module,
                    stage=stage,
                    key=action_key(state, module, stage),
                    reason=reason,
                )
            )
    return actions


def plan_actions(
    previous: Optional[GraphState],
    current: GraphState,
) -> ActionPlan:
    current_names = {module.name for module in current.modules}
    if previous is None:
        actions = _actions_for(current, current_names, ACTION_STAGES, "no-state")
        return ActionPlan(tuple(actions), True, "no-state")
    previous_names = {module.name for module in previous.modules}
    if previous_names != current_names:
        actions = _actions_for(
            current,
            current_names,
            ACTION_STAGES,
            "module-set-changed",
        )
        return ActionPlan(tuple(actions), True, "module-set-changed")
    if _global_identity(previous) != _global_identity(current):
        actions = _actions_for(
            current,
            current_names,
            ACTION_STAGES,
            "global-identity-changed",
        )
        return ActionPlan(tuple(actions), True, "global-identity-changed")

    old = previous.module_map()
    new = current.module_map()
    source_changed: set[str] = set()
    summary_changed: set[str] = set()
    graph_changed: set[str] = set()
    for name in sorted(current_names):
        if old[name].source_digest != new[name].source_digest:
            source_changed.add(name)
        if old[name].summary.digest() != new[name].summary.digest():
            summary_changed.add(name)
        if old[name].dependencies != new[name].dependencies:
            graph_changed.add(name)
    if not source_changed and not summary_changed and not graph_changed:
        return ActionPlan((), False, "cache-hit")

    actions: list[Action] = []
    actions.extend(
        _actions_for(
            current,
            source_changed,
            ACTION_STAGES,
            "source-changed",
        )
    )
    reverse = _reverse_edges((previous, current))
    public_roots = summary_changed | graph_changed
    dependent_closure = _reverse_closure(public_roots, reverse) - source_changed
    actions.extend(
        _actions_for(
            current,
            dependent_closure,
            _DEPENDENT_STAGES,
            "public-summary-changed",
        )
    )
    unique: dict[tuple[str, str], Action] = {}
    for action in actions:
        unique[(action.module, action.stage)] = action
    ordered = sorted(
        unique.values(),
        key=lambda action: (action.module, ACTION_STAGES.index(action.stage)),
    )
    return ActionPlan(tuple(ordered), False, "incremental")


def load_graph_state(payload: Any) -> Optional[GraphState]:
    """Parse persisted state; any unknown/corrupt shape is a safe miss."""
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "compiler_digest",
            "modules",
            "options_digest",
            "runtime_abi_digest",
            "schema",
            "target",
        }:
            return None
        if payload.get("schema") != SCHEMA:
            return None
        modules: list[ModuleState] = []
        raw_modules = payload.get("modules")
        if not isinstance(raw_modules, list):
            return None
        for raw in raw_modules:
            if not isinstance(raw, dict) or set(raw) != {
                "dependencies",
                "name",
                "source_digest",
                "summary",
                "summary_digest",
            }:
                return None
            summary_raw = raw.get("summary")
            if not isinstance(summary_raw, dict) or set(summary_raw) != {
                "effects",
                "exports",
                "imported_types",
                "layouts",
            }:
                return None
            if not isinstance(raw.get("dependencies"), list):
                return None
            for field in ("effects", "exports", "imported_types", "layouts"):
                if not isinstance(summary_raw.get(field), list):
                    return None
            summary = PublicSummary.create(
                imported_types=summary_raw["imported_types"],
                exports=summary_raw["exports"],
                effects=summary_raw["effects"],
                layouts=summary_raw["layouts"],
            )
            if raw.get("summary_digest") != summary.digest():
                return None
            modules.append(
                ModuleState.create(
                    str(raw["name"]),
                    str(raw["source_digest"]),
                    raw["dependencies"],
                    summary,
                )
            )
        return GraphState.create(
            compiler_digest=str(payload["compiler_digest"]),
            runtime_abi_digest=str(payload["runtime_abi_digest"]),
            target=str(payload["target"]),
            options_digest=str(payload["options_digest"]),
            modules=modules,
        )
    except (KeyError, TypeError, ValueError):
        return None


def plan_from_payload(payload: Any, current: GraphState) -> ActionPlan:
    previous = load_graph_state(payload)
    if previous is None:
        actions = _actions_for(
            current,
            (module.name for module in current.modules),
            ACTION_STAGES,
            "invalid-state",
        )
        return ActionPlan(tuple(actions), True, "invalid-state")
    return plan_actions(previous, current)


def load_graph_state_file(root: str) -> Optional[GraphState]:
    """Load the manifest-last graph receipt, treating every defect as a miss."""

    state_path = os.path.join(os.path.abspath(root), "state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as stream:
            envelope = json.load(stream)
        if not isinstance(envelope, dict) or set(envelope) != {
            "graph_digest",
            "payload",
            "schema",
        }:
            return None
        if envelope.get("schema") != SCHEMA:
            return None
        payload = envelope.get("payload")
        state = load_graph_state(payload)
        if state is None or envelope.get("graph_digest") != state.digest():
            return None
        return state
    except (OSError, TypeError, ValueError):
        return None


def publish_graph_state_file(root: str, state: GraphState) -> bool:
    """Atomically publish the graph receipt after all actions are durable."""

    root = os.path.abspath(root)
    staging = ""
    try:
        os.makedirs(root, exist_ok=True)
        staging = tempfile.mkdtemp(prefix="state.tmp.", dir=root)
        staged_path = os.path.join(staging, "state.json")
        envelope = {
            "graph_digest": state.digest(),
            "payload": state.payload(),
            "schema": SCHEMA,
        }
        with open(staged_path, "w", encoding="utf-8") as stream:
            json.dump(envelope, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged_path, os.path.join(root, "state.json"))
        os.rmdir(staging)
        return load_graph_state_file(root) == state
    except OSError:
        try:
            if staging and os.path.isdir(staging):
                shutil.rmtree(staging)
        except OSError:
            pass
        return False


def _artifact_paths(root: str, key: str) -> tuple[str, str, str]:
    entry = os.path.join(os.path.abspath(root), "actions", key[:2], key)
    return entry, os.path.join(entry, "manifest.json"), os.path.join(
        entry,
        "artifact.bin",
    )


def load_action_artifact(root: str, action: Action) -> Optional[bytes]:
    _entry, manifest_path, artifact_path = _artifact_paths(root, action.key)
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or set(manifest) != {
            "artifact_sha256",
            "key",
            "module",
            "schema",
            "stage",
        }:
            return None
        if (
            manifest.get("schema") != SCHEMA
            or manifest.get("key") != action.key
            or manifest.get("module") != action.module
            or manifest.get("stage") != action.stage
        ):
            return None
        with open(artifact_path, "rb") as stream:
            artifact = stream.read()
        if hashlib.sha256(artifact).hexdigest() != manifest.get("artifact_sha256"):
            return None
        return artifact
    except (OSError, TypeError, ValueError):
        return None


def publish_action_artifact(root: str, action: Action, artifact: bytes) -> bool:
    if not isinstance(artifact, bytes) or not artifact:
        return False
    entry, _manifest_path, _artifact_path = _artifact_paths(root, action.key)
    existing = load_action_artifact(root, action)
    if existing is not None:
        return existing == artifact
    parent = os.path.dirname(entry)
    staging = ""
    try:
        os.makedirs(parent, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=action.key + ".tmp.", dir=parent)
        artifact_path = os.path.join(staging, "artifact.bin")
        manifest_path = os.path.join(staging, "manifest.json")
        with open(artifact_path, "wb") as stream:
            stream.write(artifact)
            stream.flush()
            os.fsync(stream.fileno())
        manifest = {
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
            "key": action.key,
            "module": action.module,
            "schema": SCHEMA,
            "stage": action.stage,
        }
        with open(manifest_path, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.rename(staging, entry)
        except FileExistsError:
            shutil.rmtree(staging)
        return load_action_artifact(root, action) == artifact
    except OSError:
        try:
            if staging and os.path.isdir(staging):
                shutil.rmtree(staging)
        except OSError:
            pass
        return False


__all__ = [
    "ACTION_STAGES",
    "Action",
    "ActionPlan",
    "GraphState",
    "ModuleState",
    "PublicSummary",
    "SCHEMA",
    "action_key",
    "load_graph_state",
    "load_graph_state_file",
    "load_action_artifact",
    "plan_actions",
    "plan_from_payload",
    "publish_action_artifact",
    "publish_graph_state_file",
    "source_digest",
]
