"""The repository's own tooling is Python: no shell script may come back.

``scripts/bootstrap.sh`` and its ten siblings (native-deferred wrapper,
no-libpython ratchet, closure gates, GC long-run/contract runners, docker
harness) were ported to Python so the build, the gates and the harnesses run
on macOS, Linux and Windows without bash, ``uname``, ``codesign``, ``cmp`` or
BSD ``stat``.  Vendored projects under ``projects/`` keep their upstream
scripts; they are not this repository's tooling.

Three scripts still *emit or require* a shell launcher for generated
artifacts.  They are listed explicitly so a new shell dependency fails this
guard instead of arriving unnoticed; converting them is tracked separately
(they are release-installer / Darwin A/B helpers, not the build entry).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).absolute().parents[2]
_VENDORED = ("projects/",)

_PYTHON_ENTRYPOINTS = (
    "scripts/bootstrap.py",
    "scripts/run_pcc_native_deferred.py",
    "scripts/bootstrap_platform.py",
    "scripts/verify_nolibpython.py",
    "scripts/run_test_gates.py",
    "scripts/run_self_backend_linux_x86_64_docker.py",
    "scripts/file_lock.py",
)

# Generated-launcher / required-tool sites that still name a shell.
_KNOWN_SHELL_PRODUCERS = frozenset(
    {
        "install_pcc1_toolchain.py",  # generated host-pcc + launcher scripts
        "probe_pcc1_self_runtime.py",  # Darwin probe helper
        "run_pcc_compile_ab.py",  # records /bin/sh as a required external tool
    }
)


def _tracked_shell_scripts() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    return [
        line
        for line in listed.splitlines()
        if line and not line.startswith(_VENDORED)
    ]


def _shell_scripts_on_disk() -> list[str]:
    """Untracked shell scripts count too: an A/B harness that resurrects one
    under ``scripts/`` is the same regression as committing it.
    """

    found = []
    for root in ("scripts", "tests", "pcc", "utils"):
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.sh"):
            relative = path.relative_to(REPO).as_posix()
            if "__pycache__" in relative:
                continue
            found.append(relative)
    found.extend(
        path.name for path in REPO.glob("*.sh")
    )
    return sorted(found)


def test_no_repository_shell_scripts():
    assert _tracked_shell_scripts() == [], (
        "repository tooling must be Python; vendored projects/ may keep theirs"
    )


def test_no_shell_scripts_on_disk_even_untracked():
    assert _shell_scripts_on_disk() == [], (
        "a shell script reappeared in the repository's own trees (tracked or "
        "not); measurement harnesses must be Python too"
    )


def test_python_build_entrypoints_exist():
    for name in _PYTHON_ENTRYPOINTS:
        assert (REPO / name).is_file(), name


def test_build_entrypoints_never_launch_a_shell():
    offenders = []
    for name in _PYTHON_ENTRYPOINTS:
        text = (REPO / name).read_text(encoding="utf-8")
        if "/bin/bash" in text or "/bin/sh" in text or '"bash"' in text:
            offenders.append(name)
    assert offenders == [], offenders


def test_shell_launcher_producers_are_the_known_list():
    found = {
        path.name
        for path in sorted((REPO / "scripts").glob("*.py"))
        if "/bin/bash" in path.read_text(encoding="utf-8")
        or "/bin/sh" in path.read_text(encoding="utf-8")
    }
    assert found == set(_KNOWN_SHELL_PRODUCERS), (
        "a script gained or lost a shell dependency; update the migration list"
    )


def test_watchdog_still_forbids_shell_execution_owners():
    """The native-stage supervisor's forbidden-owner list stays intact."""

    text = (REPO / "scripts" / "platform_process_watchdog.py").read_text(
        encoding="utf-8"
    )
    for owner in ('"sh"', '"bash"', '"cmd.exe"', '"powershell.exe"'):
        assert owner in text
