from pathlib import Path

from pcc.frontends.python import pipeline


def test_darwin_self_backend_signs_temp_executable_before_publish_move():
    src = Path("pcc/frontends/python/pipeline_self_backend_link.py").read_text(encoding="utf-8")

    sign_tmp = '["/usr/bin/codesign", "--force", "-s", "-", tmp_out_path]'
    verify_tmp = '["/usr/bin/codesign", "--verify", tmp_out_path]'
    publish_move = 'os.replace(tmp_out_path, out_path)'
    verify_final = '["/usr/bin/codesign", "--verify", out_path]'

    sign_idx = src.index(sign_tmp)
    verify_tmp_idx = src.index(verify_tmp)
    move_idx = src.index(publish_move)
    verify_final_idx = src.index(verify_final)

    assert sign_idx < move_idx
    assert verify_tmp_idx < move_idx
    assert move_idx < verify_final_idx


def test_darwin_self_backend_publish_sync_defaults_to_correctness(monkeypatch):
    monkeypatch.delenv("PCC_SELF_BACKEND_PUBLISH_SYNC", raising=False)
    assert pipeline._self_backend_publish_sync_enabled() is True

    monkeypatch.setenv("PCC_SELF_BACKEND_PUBLISH_SYNC", "0")
    assert pipeline._self_backend_publish_sync_enabled() is False

    monkeypatch.setenv("PCC_SELF_BACKEND_PUBLISH_SYNC", "off")
    assert pipeline._self_backend_publish_sync_enabled() is False

    monkeypatch.setenv("PCC_SELF_BACKEND_PUBLISH_SYNC", "False")
    assert pipeline._self_backend_publish_sync_enabled() is False

    monkeypatch.setenv("PCC_SELF_BACKEND_PUBLISH_SYNC", "1")
    assert pipeline._self_backend_publish_sync_enabled() is True


def test_bootstrap_stage_barrier_exec_smokes_published_stage_binary(
    tmp_path, monkeypatch
):
    """The native execution gate runs on every host, not only Darwin."""

    import subprocess as subprocess_module

    from scripts import bootstrap

    options = bootstrap.Options({"PCC_BOOTSTRAP_OUT_DIR": str(tmp_path)})
    options.out_dir.mkdir(parents=True, exist_ok=True)
    stage_binary = tmp_path / "pcc1"
    stage_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stage_binary.chmod(0o755)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append([str(part) for part in command])
        stderr = kwargs.get("stderr")
        if hasattr(stderr, "write"):
            stderr.write(b"")
        return subprocess_module.CompletedProcess(command, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    assert bootstrap.stage_exec_barrier(stage_binary, 1, options) == 0

    assert calls[0][0] == str(stage_binary)
    assert calls[0][1] == "--help"
    smoke = calls[1]
    assert "--ir-scaffold=on" in smoke
    assert smoke[smoke.index("--backend") + 1] == "self"
    assert smoke[smoke.index("--python-libpython") + 1] == "off"
    assert calls[2] == [smoke[smoke.index("-o") + 1]]
    assert not list(options.out_dir.glob("stage-smoke.*")), "smoke dir left behind"


def test_self_backend_keeps_lc_uuid_and_requires_byte_identity():
    """The emitter keeps the real UUID; the gate requires identity."""

    pipeline_src = Path("pcc/frontends/python/pipeline.py").read_text(encoding="utf-8")
    bootstrap_src = Path("scripts/bootstrap.py").read_text(encoding="utf-8")

    assert "-Wl,-no_uuid" not in pipeline_src
    # No normalized acceptance path: the compare is bytes-or-fail.
    assert "normalize_image" not in bootstrap_src
    assert "byte-identical" in bootstrap_src
    assert "no normalized acceptance path" in bootstrap_src
