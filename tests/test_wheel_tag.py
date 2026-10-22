"""Release wheels carry ``py3-none-<platform>``, not the building interpreter's tag.

``infer_tag`` named the wheel after the build interpreter (``cp313-cp313`` only
installs on 3.13) and the build host (``macosx_26_0``, ``linux_x86_64``), which
the Linux wheel gate rejects. The wheel has no CPython extension module, and
the platform part follows what pcc1 itself requires.
"""

from __future__ import annotations

import pytest

from tests.test_runtime_archive_consumers import _load_hatch_build


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    (
        ("darwin", "arm64", "py3-none-macosx_12_0_arm64"),
        ("darwin", "x86_64", "py3-none-macosx_12_0_x86_64"),
        ("linux", "x86_64", "py3-none-manylinux_2_17_x86_64"),
        ("linux", "aarch64", "py3-none-manylinux_2_17_aarch64"),
        ("win32", "AMD64", "py3-none-win_amd64"),
    ),
)
def test_native_wheel_tag_follows_pcc1_platform(monkeypatch, system, machine, expected):
    module = _load_hatch_build(monkeypatch)
    monkeypatch.setattr(module.sys, "platform", system)
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    assert module._native_wheel_tag() == expected


def test_native_wheel_tag_rejects_an_unknown_platform(monkeypatch):
    module = _load_hatch_build(monkeypatch)
    monkeypatch.setattr(module.sys, "platform", "freebsd14")
    monkeypatch.setattr(module.platform, "machine", lambda: "amd64")
    with pytest.raises(RuntimeError, match="no native wheel tag"):
        module._native_wheel_tag()


def test_sdist_target_builds_neither_runtime_nor_pcc1(monkeypatch, tmp_path):
    from tests.test_runtime_archive_consumers import _new_build_hook

    module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    (root / "pcc" / "runtime").mkdir(parents=True)
    hook = _new_build_hook(module, root)
    hook.target_name = "sdist"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an sdist build must not construct release artifacts")

    monkeypatch.setattr(hook, "_run_make", forbidden)
    monkeypatch.setattr(hook, "_run_pcc_self_compile", forbidden)
    monkeypatch.delenv("PCC_BUILD_SKIP", raising=False)
    build_data = {}
    hook.initialize("standard", build_data)
    assert build_data == {}
