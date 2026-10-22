from __future__ import annotations

from pathlib import Path

import pytest

from pcc.frontends.python.vthread_effect_summary_wire import read_summary, write_summary


def test_vthread_effect_summary_wire_is_deterministic_and_roundtrips(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.wire"
    second = tmp_path / "second.wire"
    args = (
        "pkg.worker",
        ["f:pkg.worker:leaf"],
        ["f:pkg.worker:entry", "f:pkg.worker:leaf"],
        ["f:pkg.worker:entry", "f:pkg.worker:leaf"],
    )
    write_summary(str(first), *args)
    write_summary(str(second), *args)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8") == (
        "pcc.vthread.effect-summary.v1\n"
        "M\tpkg.worker\n"
        "S\tf:pkg.worker:leaf\n"
        "E\tf:pkg.worker:entry\tf:pkg.worker:leaf\n"
        "P\tf:pkg.worker:entry\n"
        "P\tf:pkg.worker:leaf\n"
    )
    assert read_summary(str(first)) == {
        "module_name": "pkg.worker",
        "seeds": ["f:pkg.worker:leaf"],
        "edges": ["f:pkg.worker:entry", "f:pkg.worker:leaf"],
        "publish": ["f:pkg.worker:entry", "f:pkg.worker:leaf"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        "wrong\nM\tpkg.worker\n",
        "pcc.vthread.effect-summary.v1\n",
        "pcc.vthread.effect-summary.v1\nM\ta\nM\tb\n",
        "pcc.vthread.effect-summary.v1\nM\ta\nE\tcaller\n",
        "pcc.vthread.effect-summary.v1\nM\ta\nX\tvalue\n",
        "pcc.vthread.effect-summary.v1\nM\ta\nS\t\n",
    ],
)
def test_vthread_effect_summary_wire_rejects_malformed_rows(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "malformed.wire"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        read_summary(str(path))


def test_vthread_effect_summary_writer_validates_before_publish(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.wire"
    with pytest.raises(ValueError, match="edge payload is odd"):
        write_summary(str(path), "pkg.worker", [], ["caller"], [])
    assert not path.exists()
