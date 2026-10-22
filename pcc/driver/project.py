"""C project builder: collect and merge multiple .c files for compilation.

Usage:
    pcc <file.c>      - compile and run a single file
    pcc <directory>    - collect all .c files, merge, compile and run

For a directory project, the builder:
1. Finds all .c files in the directory (non-recursive)
2. Puts the file containing main() last
3. Merges all source files into a single compilation unit
4. #include "xxx.h" is resolved by the preprocessor
"""

import os
import re
import shlex
import shutil
import subprocess
import warnings
from dataclasses import dataclass


@dataclass(frozen=True)
class TranslationUnit:
    name: str
    path: str
    source: str


@dataclass(frozen=True)
class ProjectDependency:
    path: str
    sources_from_make: str | None = None


@dataclass(frozen=True)
class MakeGoalSpec:
    path: str
    goal: str


_MAKE_CPP_TWO_TOKEN_FLAGS = {"-D", "-U", "-I", "-include", "-isystem", "-iquote", "-idirafter"}
_MAKE_CPP_PATH_FLAGS = {"-I", "-include", "-isystem", "-iquote", "-idirafter"}
_MAKE_CPP_PREFIX_FLAGS = ("-D", "-U", "-I", "-include", "-isystem", "-iquote", "-idirafter")
_MAKE_ASSIGN_RE = re.compile(
    r"^(?:override\s+)?([A-Za-z0-9_.-]+)\s*([:+?]?=)\s*(.*)$"
)
_MAKE_VAR_REF_RE = re.compile(r"\$\(([^()]+)\)|\${([^{}]+)}")
_AUTOCONF_PLACEHOLDER_RE = re.compile(r"@[A-Za-z0-9_]+@")


def collect_project(path, sources_from_make=None, cpp_args=None):
    """Collect C source from a file or directory.

    Args:
        path: path to a .c file or a directory
        sources_from_make: optional make goal used to derive participating .c files
        cpp_args: optional extra preprocessor args used while locating main()

    Returns:
        (merged_source, base_dir)
    """
    path = os.path.abspath(path)

    if os.path.isfile(path):
        if sources_from_make:
            raise ValueError("--sources-from-make requires a directory input")
        base_dir = os.path.dirname(path)
        with open(path, 'r') as f:
            return f.read(), base_dir

    if os.path.isdir(path):
        return (
            _collect_directory(
                path,
                sources_from_make=sources_from_make,
                cpp_args=_main_detection_cpp_args(
                    path,
                    sources_from_make=sources_from_make,
                    cpp_args=cpp_args,
                ),
            ),
            path,
        )

    raise FileNotFoundError(f"Not found: {path}")


def collect_translation_units(path, sources_from_make=None, dependencies=None, cpp_args=None):
    """Collect translation units from a file or directory.

    When dependencies are provided, the primary input must contribute the single
    program entrypoint and dependency inputs must not define `main()`.

    Returns:
        (list[TranslationUnit], base_dir)
    """
    return _collect_translation_units_with_dependencies(
        path,
        sources_from_make=sources_from_make,
        dependencies=dependencies,
        cpp_args=_main_detection_cpp_args(
            path,
            sources_from_make=sources_from_make,
            dependencies=dependencies,
            cpp_args=cpp_args,
        ),
    )


def collect_cpp_args(path, sources_from_make=None, dependencies=None):
    """Collect preprocessor args implied by make-derived source selection.

    This is intentionally limited to CPP-ish flags that can be forwarded to
    `_system_cpp`, such as `-D`, `-U`, `-I`, and `-include`.
    """
    path = os.path.abspath(path)
    dependency_specs = parse_dependency_specs(dependencies)

    arg_groups = []
    arg_groups.extend(_collect_input_cpp_arg_groups(path, sources_from_make=sources_from_make))
    for dep in dependency_specs:
        arg_groups.extend(
            _collect_input_cpp_arg_groups(
                dep.path,
                sources_from_make=dep.sources_from_make,
            )
        )

    flattened = []
    seen = set()
    for group in arg_groups:
        key = tuple(group)
        if key in seen:
            continue
        seen.add(key)
        flattened.extend(group)
    return flattened


def _main_detection_cpp_args(
    path,
    sources_from_make=None,
    dependencies=None,
    cpp_args=None,
):
    # Use the same macro/include context as the real compile so main()
    # detection does not warn on expected project-specific preprocess state.
    return tuple(
        collect_cpp_args(
            path,
            sources_from_make=sources_from_make,
            dependencies=dependencies,
        )
    ) + tuple(cpp_args or ())


def parse_dependency_specs(specs):
    deps = []
    for spec in specs or []:
        dep_path = spec
        dep_goal = None
        if "=" in spec:
            dep_path, dep_goal = spec.rsplit("=", 1)
        dep_path = dep_path.strip()
        dep_goal = dep_goal.strip() or None if dep_goal is not None else None
        if not dep_path:
            raise ValueError("empty dependency path in --depends-on")
        deps.append(
            ProjectDependency(path=os.path.abspath(dep_path), sources_from_make=dep_goal)
        )
    return deps


def parse_make_goal_specs(specs, option_name="--ensure-make-goal"):
    goals = []
    for spec in specs or []:
        path, sep, goal = spec.rpartition("=")
        path = path.strip()
        goal = goal.strip()
        if not sep or not path or not goal:
            raise ValueError(f"{option_name} entries must look like PATH=GOAL")
        abspath = os.path.abspath(path)
        if not os.path.isdir(abspath):
            raise FileNotFoundError(f"Not found: {abspath}")
        goals.append(MakeGoalSpec(path=abspath, goal=goal))
    return goals


def run_prepare_commands(commands):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    for command in commands or []:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            continue
        detail = (result.stderr or result.stdout or "unknown error").strip()
        if len(detail) > 400:
            detail = detail[:400]
        raise RuntimeError(f"prepare command failed: {command}\n{detail}")


def ensure_make_goals(specs, jobs=2):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    for spec in parse_make_goal_specs(specs):
        result = subprocess.run(
            ["make", "-C", spec.path, spec.goal, f"-j{max(1, jobs)}"],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            continue
        detail = (result.stderr or result.stdout or "unknown error").strip()
        if len(detail) > 400:
            detail = detail[:400]
        raise RuntimeError(
            f"failed to build make goal '{spec.goal}' in {spec.path}: {detail}"
        )


def translation_unit_include_dirs(units):
    dirs = []
    seen = set()
    for unit in units:
        unit_dir = os.path.abspath(os.path.dirname(unit.path))
        if unit_dir not in seen:
            seen.add(unit_dir)
            dirs.append(unit_dir)
    return dirs


def _collect_translation_units_with_dependencies(
    path, sources_from_make=None, dependencies=None, cpp_args=None
):
    path = os.path.abspath(path)
    dependency_specs = parse_dependency_specs(dependencies)

    primary_units, base_dir = _collect_input_units(
        path, sources_from_make=sources_from_make
    )
    if not dependency_specs:
        if os.path.isfile(path):
            return primary_units, base_dir
        return (
            _collect_directory_units(
                path,
                sources_from_make=sources_from_make,
                cpp_args=cpp_args,
            ),
            base_dir,
        )

    dependency_units = []
    for dep in dependency_specs:
        dep_units, _ = _collect_input_units(
            dep.path, sources_from_make=dep.sources_from_make
        )
        dep_main_units, dep_other_units = _split_main_units(dep_units, cpp_args=cpp_args)
        if dep_main_units:
            names = ", ".join(unit.name for unit in dep_main_units)
            raise ValueError(
                f"Dependency input {dep.path} must not define main(): {names}"
            )
        dependency_units.extend(dep_other_units)

    primary_main_units, primary_other_units = _split_main_units(
        primary_units,
        cpp_args=cpp_args,
    )
    if os.path.isfile(path):
        if len(primary_main_units) != 1:
            raise ValueError(
                f"Primary file {path} must define exactly one main() when using --depends-on"
            )
    else:
        if not primary_main_units:
            raise ValueError(f"No main() function found in {path}/*.c")
        if len(primary_main_units) > 1:
            names = ", ".join(unit.name for unit in primary_main_units)
            raise ValueError(f"Multiple main() definitions found in {path}: {names}")

    return dependency_units + primary_other_units + primary_main_units, base_dir


def _collect_directory(dirpath, sources_from_make=None, cpp_args=None):
    """Merge all .c files in a directory into one source string."""
    c_files = _project_c_files(dirpath, sources_from_make=sources_from_make)

    if not c_files:
        raise FileNotFoundError(f"No .c files found in {dirpath}")

    # Separate: main file goes last, others go first
    main_file = None
    other_files = []

    for fname in c_files:
        fpath = _resolve_source_path(dirpath, fname)
        with open(fpath, 'r') as f:
            content = f.read()
        if _has_main(content, fpath, include_dirs=[dirpath], cpp_args=cpp_args):
            main_file = (fname, content)
        else:
            other_files.append((fname, content))

    if main_file is None:
        raise ValueError(f"No main() function found in {dirpath}/*.c")

    # Merge: other files first, then main file
    parts = []
    for fname, content in other_files:
        parts.append(f"// --- {fname} ---")
        parts.append(content)
    fname, content = main_file
    parts.append(f"// --- {fname} (main) ---")
    parts.append(content)

    return '\n'.join(parts)


def _collect_directory_units(dirpath, sources_from_make=None, cpp_args=None):
    """Collect a directory as independent translation units."""
    units, _ = _collect_input_units(dirpath, sources_from_make=sources_from_make)
    main_files, other_units = _split_main_units(units, cpp_args=cpp_args)

    if not main_files:
        raise ValueError(f"No main() function found in {dirpath}/*.c")
    if len(main_files) > 1:
        names = ", ".join(unit.name for unit in main_files)
        raise ValueError(f"Multiple main() definitions found in {dirpath}: {names}")

    return other_units + main_files


def _collect_input_units(path, sources_from_make=None):
    if os.path.isfile(path):
        if sources_from_make:
            raise ValueError("--sources-from-make requires a directory input")
        base_dir = os.path.dirname(path)
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            return [TranslationUnit(os.path.basename(path), path, f.read())], base_dir

    if os.path.isdir(path):
        c_files = _project_c_files(path, sources_from_make=sources_from_make)
        if not c_files:
            raise FileNotFoundError(f"No .c files found in {path}")

        units = []
        for fname in c_files:
            fpath = _resolve_source_path(path, fname)
            with open(fpath, "r", encoding="utf-8", errors="surrogateescape") as f:
                units.append(TranslationUnit(fname, fpath, f.read()))
        return units, path

    raise FileNotFoundError(f"Not found: {path}")


def _collect_input_cpp_arg_groups(path, sources_from_make=None):
    if os.path.isfile(path):
        if sources_from_make:
            raise ValueError("--sources-from-make requires a directory input")
        return []

    if os.path.isdir(path):
        if not sources_from_make:
            return []
        return _make_goal_cpp_arg_groups(path, sources_from_make)

    raise FileNotFoundError(f"Not found: {path}")


def _split_main_units(units, cpp_args=None):
    main_units = []
    other_units = []
    include_dirs = translation_unit_include_dirs(units)
    for unit in units:
        if _has_main(
            unit.source,
            unit.path,
            include_dirs=include_dirs,
            cpp_args=cpp_args,
        ):
            main_units.append(unit)
        else:
            other_units.append(unit)
    return main_units, other_units


def _project_c_files(dirpath, sources_from_make=None):
    if sources_from_make:
        return _make_goal_c_files(dirpath, sources_from_make)
    return _directory_c_files(dirpath)


def _directory_c_files(dirpath):
    return sorted(f for f in os.listdir(dirpath) if f.endswith(".c"))


def _make_goal_c_files(dirpath, goal):
    sources, _cpp_arg_groups = _scan_make_goal(dirpath, goal)
    return sources


def _make_goal_cpp_arg_groups(dirpath, goal):
    _sources, cpp_arg_groups = _scan_make_goal(dirpath, goal)
    return cpp_arg_groups


def _scan_make_goal(dirpath, goal):
    make = shutil.which("make")
    if not make:
        raise ValueError("make not found in PATH")

    # Try the cheapest dry-run first. If the goal is already up to date, a
    # plain `make -n goal` may not emit any compile commands, so fall back to
    # `make -n clean goal`: dry-run means nothing is actually deleted, but make
    # still prints the rebuild commands from a clean state. Keep `-nB` last,
    # because forcing every prerequisite can trigger expensive or brittle
    # reconfiguration rules in real project trees.
    attempts = [
        (("-n",), ()),
        (("-n",), ("clean",)),
        (("-nB",), ()),
    ]
    last_error = None

    makefiles = [None]
    for candidate in ("Makefile.in", "makefile.in"):
        if os.path.isfile(os.path.join(dirpath, candidate)):
            makefiles.append(candidate)

    for makefile in makefiles:
        for dry_run_flags, pre_goals in attempts:
            cmd = [make, *dry_run_flags, "-C", dirpath]
            if makefile is not None:
                cmd += ["-f", makefile]
            cmd += list(pre_goals)
            cmd.append(goal)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                last_error = result.stderr.strip() or result.stdout.strip()
                continue

            sources = []
            source_seen = set()
            cpp_arg_groups = []
            cpp_seen = set()
            for line in result.stdout.splitlines():
                line_sources = _extract_c_sources_from_make_line(line, dirpath)
                for source_path in line_sources:
                    source_key = os.path.abspath(
                        _resolve_source_path(dirpath, source_path)
                    )
                    if source_key not in source_seen:
                        source_seen.add(source_key)
                        sources.append(source_path)
                if not line_sources:
                    continue
                for cpp_group in _extract_cpp_args_from_make_line(line, dirpath):
                    key = tuple(cpp_group)
                    if key not in cpp_seen:
                        cpp_seen.add(key)
                        cpp_arg_groups.append(cpp_group)

            if sources:
                if not cpp_arg_groups:
                    for cpp_group in _probe_make_cpp_arg_groups(
                        make,
                        dirpath,
                        makefile,
                        sources,
                    ):
                        key = tuple(cpp_group)
                        if key not in cpp_seen:
                            cpp_seen.add(key)
                            cpp_arg_groups.append(cpp_group)
                return sources, cpp_arg_groups

            via = f" via {makefile}" if makefile is not None else ""
            pre = f" after {' '.join(pre_goals)}" if pre_goals else ""
            last_error = (
                f"make goal '{goal}'{via}{pre} did not yield any C compilation commands in {dirpath}"
            )

    fallback_sources, fallback_cpp_arg_groups = _scan_make_goal_fallback(dirpath, goal)
    if fallback_sources:
        return fallback_sources, fallback_cpp_arg_groups

    raise ValueError(
        f"failed to collect C sources from make goal '{goal}': {last_error}"
    )


def _scan_make_goal_fallback(dirpath, goal):
    primary_makefile = None
    for candidate in ("Makefile", "makefile", "GNUmakefile", "Makefile.in", "makefile.in"):
        candidate_path = os.path.join(dirpath, candidate)
        if os.path.isfile(candidate_path):
            primary_makefile = candidate_path
            break
    if primary_makefile is None:
        return [], []

    variables, rules = _parse_makefile_tree(primary_makefile)
    sources = _fallback_goal_sources(goal, dirpath, variables, rules)
    if not sources:
        return [], []

    cpp_arg_groups = _fallback_cpp_arg_groups(
        dirpath, os.path.dirname(primary_makefile), variables
    )
    return sources, cpp_arg_groups


def _parse_makefile_tree(path):
    variables = {
        "CURDIR": os.path.abspath(os.path.dirname(path)),
    }
    rules = {}
    visited = set()
    _parse_makefile_into(path, variables, rules, visited)
    return variables, rules


def _parse_makefile_into(path, variables, rules, visited):
    path = os.path.abspath(path)
    if path in visited or not os.path.isfile(path):
        return
    visited.add(path)

    variables.setdefault("CURDIR", os.path.abspath(os.path.dirname(path)))
    variables["MAKEFILE_LIST"] = f"{variables.get('MAKEFILE_LIST', '')} {path}".strip()

    active = True
    condition_stack = []

    for line in _read_makefile_logical_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[0].isspace():
            continue

        if stripped.startswith("ifdef "):
            name = stripped.split(None, 1)[1].strip()
            cond = bool(_make_expand(name and f"$({name})", variables, os.path.dirname(path)).strip())
            condition_stack.append((active, cond))
            active = active and cond
            continue
        if stripped.startswith("ifndef "):
            name = stripped.split(None, 1)[1].strip()
            cond = not bool(_make_expand(name and f"$({name})", variables, os.path.dirname(path)).strip())
            condition_stack.append((active, cond))
            active = active and cond
            continue
        if stripped.startswith("ifeq "):
            cond = _evaluate_make_condition(stripped[5:].strip(), variables, os.path.dirname(path), expect_equal=True)
            condition_stack.append((active, cond))
            active = active and cond
            continue
        if stripped.startswith("ifneq "):
            cond = _evaluate_make_condition(stripped[6:].strip(), variables, os.path.dirname(path), expect_equal=False)
            condition_stack.append((active, cond))
            active = active and cond
            continue
        if stripped == "else" or stripped.startswith("else #"):
            if condition_stack:
                parent_active, cond = condition_stack[-1]
                active = parent_active and (not cond)
                condition_stack[-1] = (parent_active, not cond)
            continue
        if stripped == "endif" or stripped.startswith("endif #"):
            if condition_stack:
                parent_active, _cond = condition_stack.pop()
                active = parent_active
            continue

        if not active:
            continue

        if stripped.startswith("-include "):
            include_expr = stripped[len("-include "):].strip()
            include_path = _resolve_make_include_path(include_expr, os.path.dirname(path), variables)
            if include_path is not None:
                _parse_makefile_into(include_path, variables, rules, visited)
            continue
        if stripped.startswith("include "):
            include_expr = stripped[len("include "):].strip()
            include_path = _resolve_make_include_path(include_expr, os.path.dirname(path), variables)
            if include_path is not None:
                _parse_makefile_into(include_path, variables, rules, visited)
            continue

        assign_match = _MAKE_ASSIGN_RE.match(stripped)
        if assign_match:
            name, op, value = assign_match.groups()
            _apply_make_assignment(name, op, value, variables, os.path.dirname(path))
            continue

        if ":" in stripped:
            target_part, prereq_part = stripped.split(":", 1)
            targets = [
                _make_expand(token, variables, os.path.dirname(path)).strip()
                for token in target_part.split()
            ]
            prerequisites = _tokenize_make_words(
                _make_expand(prereq_part, variables, os.path.dirname(path))
            )
            for target in targets:
                if not target:
                    continue
                rules.setdefault(target, []).extend(prerequisites)


def _read_makefile_logical_lines(path):
    lines = []
    pending = ""
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if pending:
                line = pending + line.lstrip()
                pending = ""
            if line.endswith("\\"):
                pending = line[:-1] + " "
                continue
            lines.append(line)
    if pending:
        lines.append(pending)
    return lines


def _apply_make_assignment(name, op, value, variables, base_dir):
    current = variables.get(name, "")
    if op == "?=":
        if current:
            return
        variables[name] = value.strip()
        return
    if op == ":=":
        variables[name] = _make_expand(value.strip(), variables, base_dir)
        return
    if op == "+=":
        appended = _make_expand(value.strip(), variables, base_dir)
        variables[name] = f"{current} {appended}".strip() if current else appended
        return
    variables[name] = value.strip()


def _evaluate_make_condition(expr, variables, base_dir, expect_equal):
    expr = expr.strip()
    if expr.startswith("(") and expr.endswith(")") and "," in expr:
        left, right = expr[1:-1].split(",", 1)
    else:
        parts = expr.split(None, 1)
        if len(parts) != 2:
            return False
        left, right = parts
    left = _make_expand(left.strip(), variables, base_dir).strip()
    right = _make_expand(right.strip(), variables, base_dir).strip()
    return (left == right) if expect_equal else (left != right)


def _resolve_make_include_path(include_expr, base_dir, variables):
    for token in _tokenize_make_words(_make_expand(include_expr, variables, base_dir)):
        if not token:
            continue
        candidate = token
        if not os.path.isabs(candidate):
            candidate = os.path.abspath(os.path.join(base_dir, candidate))
        if os.path.isfile(candidate):
            return candidate
        if os.path.isfile(candidate + ".in"):
            return candidate + ".in"
    return None


def _make_expand(value, variables, base_dir, _seen=None):
    if not value:
        return ""
    if _seen is None:
        _seen = set()

    def repl(match):
        expr = (match.group(1) or match.group(2) or "").strip()
        if not expr:
            return ""
        if expr in _seen:
            return ""
        if expr.startswith("firstword "):
            words = _tokenize_make_words(
                _make_expand(expr[len("firstword "):], variables, base_dir, _seen)
            )
            return words[0] if words else ""
        if expr.startswith("dir "):
            target = _make_expand(expr[len("dir "):], variables, base_dir, _seen).strip()
            if not target:
                return ""
            return os.path.dirname(target.rstrip("/")) + "/"
        if expr.startswith("strip "):
            return _make_expand(expr[len("strip "):], variables, base_dir, _seen).strip()
        if expr.startswith("wildcard "):
            return ""

        _seen.add(expr)
        replacement = _make_expand(variables.get(expr, ""), variables, base_dir, _seen)
        _seen.discard(expr)
        return replacement

    previous = None
    expanded = value
    while previous != expanded:
        previous = expanded
        expanded = _MAKE_VAR_REF_RE.sub(repl, expanded)
    return expanded


def _tokenize_make_words(value):
    if not value:
        return []
    try:
        return shlex.split(value, posix=True)
    except ValueError:
        return value.split()


def _fallback_goal_sources(goal, dirpath, variables, rules):
    sources = []
    seen = set()

    def add_source(candidate):
        source_path = candidate
        if candidate.endswith((".o", ".lo")):
            source_path = _infer_c_source_from_object_token(candidate, dirpath)
        if source_path is None:
            return
        resolved = _resolve_source_path(dirpath, source_path)
        source_key = os.path.abspath(resolved)
        if not os.path.isfile(resolved) or source_key in seen:
            return
        seen.add(source_key)
        if os.path.isabs(source_path):
            sources.append(source_path)
        else:
            sources.append(os.path.relpath(source_key, dirpath))

    def visit_target(target, visiting):
        if target in visiting:
            return
        visiting.add(target)
        for prereq in rules.get(target, []):
            if prereq.endswith(".c"):
                add_source(prereq)
                continue
            if prereq.endswith((".o", ".lo")):
                add_source(prereq)
                visit_target(prereq, visiting)
                continue
            if prereq in rules:
                visit_target(prereq, visiting)
        visiting.discard(target)

    visit_target(goal, set())
    if sources:
        return sources

    for var_name in ("OBJS", "OBJECTS", "SRCS", "SOURCES"):
        for token in _tokenize_make_words(
            _make_expand(variables.get(var_name, ""), variables, dirpath)
        ):
            if token.endswith((".c", ".o", ".lo")):
                add_source(token)
    return sources


def _fallback_cpp_arg_groups(dirpath, makefile_dir, variables):
    arg_groups = []
    seen = set()

    for var_name in ("CPPFLAGS", "PG_CPPFLAGS", "CFLAGS", "PTHREAD_CFLAGS"):
        expanded = _make_expand(variables.get(var_name, ""), variables, makefile_dir)
        if var_name == "PTHREAD_CFLAGS" and (
            not expanded.strip() or _AUTOCONF_PLACEHOLDER_RE.search(expanded)
        ):
            expanded = " ".join(
                _infer_cpp_args_from_autoconf(makefile_dir, "PTHREAD_CFLAGS")
            )
        for group in _extract_cpp_args_from_text(expanded, dirpath):
            key = tuple(group)
            if key not in seen:
                seen.add(key)
                arg_groups.append(group)
    return arg_groups


def _extract_cpp_args_from_text(text, dirpath):
    if not text:
        return []
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()

    groups = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _MAKE_CPP_TWO_TOKEN_FLAGS:
            if i + 1 < len(tokens):
                value = tokens[i + 1]
                if token in _MAKE_CPP_PATH_FLAGS:
                    value = _normalize_make_arg_path(value, dirpath)
                groups.append((token, value))
                i += 2
                continue
            i += 1
            continue

        matched_prefix = None
        for prefix in _MAKE_CPP_PREFIX_FLAGS:
            if token.startswith(prefix) and token != prefix:
                matched_prefix = prefix
                break
        if matched_prefix is not None:
            value = token[len(matched_prefix) :]
            if matched_prefix in _MAKE_CPP_PATH_FLAGS:
                value = _normalize_make_arg_path(value, dirpath)
                groups.append((matched_prefix, value))
            else:
                groups.append((matched_prefix + value,))
        i += 1

    concrete_groups = []
    for group in groups:
        concrete = []
        for token in group:
            if any("$(" in part or "${" in part for part in token):
                concrete = []
                break
            if any(_AUTOCONF_PLACEHOLDER_RE.search(part) for part in token):
                concrete = []
                break
            concrete.append(token)
        if concrete:
            concrete_groups.append(tuple(concrete))
    return concrete_groups


def _infer_cpp_args_from_autoconf(start_dir, var_name):
    current = os.path.abspath(start_dir)
    while True:
        for candidate in ("configure.ac", "configure.in", "configure"):
            path = os.path.join(current, candidate)
            if os.path.isfile(path):
                return _extract_cpp_args_from_autoconf_file(path, var_name)
        parent = os.path.dirname(current)
        if parent == current:
            return []
        current = parent


def _extract_cpp_args_from_autoconf_file(path, var_name):
    flags = []
    seen = set()
    pattern = re.compile(rf"\b{re.escape(var_name)}\s*=\s*(.+)")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            match = pattern.search(raw)
            if not match:
                continue
            for token in re.findall(r'(-(?:D|U|I)[^ \t"\']+)', match.group(1)):
                if token not in seen:
                    seen.add(token)
                    flags.append(token)
    return flags


def _probe_make_cpp_arg_groups(make, dirpath, makefile, sources):
    groups = []
    seen = set()

    for source_path in sources:
        source_stem, _ext = os.path.splitext(source_path)
        target_candidates = [os.path.basename(source_stem) + ".o"]
        if source_stem != os.path.basename(source_stem):
            target_candidates.append(source_stem + ".o")

        for object_target in target_candidates:
            cmd = [make, "-n", "-W", source_path, "-C", dirpath]
            if makefile is not None:
                cmd += ["-f", makefile]
            cmd.append(object_target)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                continue

            found_args = False
            for line in result.stdout.splitlines():
                cpp_args = _extract_cpp_args_from_make_line(line, dirpath)
                if not cpp_args:
                    continue
                found_args = True
                for cpp_group in cpp_args:
                    key = tuple(cpp_group)
                    if key not in seen:
                        seen.add(key)
                        groups.append(cpp_group)

            if found_args:
                break

    return groups


def _extract_c_sources_from_make_line(line, dirpath):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []

    stripped = stripped.lstrip("@+-")
    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return []

    sources = []
    for token in tokens:
        if not token.startswith("-") and token.endswith(".c"):
            candidate = os.path.abspath(os.path.join(dirpath, token))
            if os.path.isfile(candidate):
                project_root = os.path.realpath(dirpath)
                candidate_real = os.path.realpath(candidate)
                try:
                    same_tree = os.path.commonpath([project_root, candidate_real]) == project_root
                except ValueError:
                    same_tree = False
                if same_tree:
                    sources.append(os.path.relpath(candidate_real, project_root))
                else:
                    sources.append(candidate)
            continue

        inferred = _infer_c_source_from_object_token(token, dirpath)
        if inferred is not None:
            sources.append(inferred)
    return sources


def _extract_cpp_args_from_make_line(line, dirpath):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []

    stripped = stripped.lstrip("@+-")
    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return []

    args = []
    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in _MAKE_CPP_TWO_TOKEN_FLAGS:
            if i + 1 < len(tokens):
                value = tokens[i + 1]
                if token in _MAKE_CPP_PATH_FLAGS:
                    value = _normalize_make_arg_path(value, dirpath)
                args.append((token, value))
                i += 2
                continue
            i += 1
            continue

        matched_prefix = None
        for prefix in _MAKE_CPP_PREFIX_FLAGS:
            if token.startswith(prefix) and token != prefix:
                matched_prefix = prefix
                break
        if matched_prefix is not None:
            value = token[len(matched_prefix) :]
            if matched_prefix in _MAKE_CPP_PATH_FLAGS:
                value = _normalize_make_arg_path(value, dirpath)
                args.append((matched_prefix, value))
            else:
                args.append((matched_prefix + value,))

        i += 1

    return args


def _infer_c_source_from_object_token(token, dirpath):
    if token.startswith("-") or not token.endswith((".o", ".lo")):
        return None

    obj_name = os.path.basename(token)
    if "_la-" in obj_name:
        obj_name = obj_name.split("_la-", 1)[1]
    stem = os.path.splitext(obj_name)[0]
    candidate = os.path.abspath(os.path.join(dirpath, stem + ".c"))
    if not os.path.isfile(candidate):
        return None
    return os.path.relpath(candidate, dirpath)


def _normalize_make_arg_path(value, dirpath):
    if not value:
        return value
    if os.path.isabs(value):
        return value
    return os.path.abspath(os.path.join(dirpath, value))


def _resolve_source_path(dirpath, source_path):
    if os.path.isabs(source_path):
        return source_path
    return os.path.join(dirpath, source_path)


def _has_main(source, path=None, include_dirs=None, cpp_args=None):
    """Check if source contains a main() function definition."""
    main_pattern = re.compile(
        r"\b(?:int|void)\s+main\s*\([^;{}]*\)\s*\{",
        re.MULTILINE | re.DOTALL,
    )
    if not main_pattern.search(source):
        return False
    if path is None:
        return True

    try:
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

        processed = CEvaluator._system_cpp(
            source,
            base_dir=os.path.dirname(path),
            include_dirs=include_dirs,
            cpp_args=cpp_args,
        )
    except Exception as exc:
        warnings.warn(
            f"pcc: preprocessing failed for main() detection in "
            f"{path!r}: {exc}; falling back to regex match",
            stacklevel=2,
        )
        return True

    return bool(main_pattern.search(processed))
