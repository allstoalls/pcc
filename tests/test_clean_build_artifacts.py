from pathlib import Path
import subprocess

from scripts.clean_build_artifacts import artifact_plan


def test_clean_plan_selects_only_untracked_ignored_outputs_outside_evidence(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".gitignore").write_text("build/\nbuild_extra/\npcc/runtime/build_py/\n.venv/\n*.o\n*.ll\n")
    files = ["build/object.o", "build/source.py", "build/run/program.exe", "build/run/result.json",
             "build/frozen-source/object.o", "build_extra/other.o", "pcc/runtime/build_py/member.o",
             ".venv/library.so", "tracked.o", "loose.ll", "notes.md"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    subprocess.run(["git", "add", "-f", "tracked.o"], cwd=tmp_path, check=True, timeout=10)
    assert [str(path.relative_to(tmp_path)) for path in artifact_plan(tmp_path)] == [
        "build/object.o", "build_extra/other.o", "loose.ll", "pcc/runtime/build_py/member.o"
    ]
    assert [str(path.relative_to(tmp_path)) for path in artifact_plan(tmp_path, "runtime")] == [
        "pcc/runtime/build_py/member.o"
    ]
    assert all((tmp_path / name).exists() for name in files)


def test_clean_plan_keeps_outputs_in_read_only_directories(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".gitignore").write_text("build/\n")
    for name in ("build/loose.ll", "build/gate/output/pcc.ll"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    frozen = tmp_path / "build/gate/output"
    frozen.chmod(0o555)
    try:
        assert [str(path.relative_to(tmp_path)) for path in artifact_plan(tmp_path)] == [
            "build/loose.ll"
        ]
    finally:
        frozen.chmod(0o755)
