from pathlib import Path

from pcc.py_frontend import pipeline


def test_darwin_self_backend_signs_temp_executable_before_publish_move():
    src = Path("pcc/py_frontend/pipeline_self_backend_link.py").read_text(encoding="utf-8")

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


def test_bootstrap_stage_barrier_exec_smokes_published_stage_binary():
    src = Path("scripts/bootstrap.sh").read_text(encoding="utf-8")

    assert 'codesign --verify "${out_exe}"' in src
    assert 'cat "${out_exe}" >/dev/null' in src
    assert '"${out_exe}" --help >/dev/null 2>&1' in src
    assert 'stage-smoke.XXXXXX' in src
    assert '--ir-scaffold=on' in src
    assert '--python-libpython "${BOOTSTRAP_PYTHON_LIBPYTHON}"' in src
    assert '"publish_barrier_returncode": int(barrier_returncode)' in src


def test_darwin_self_backend_keeps_lc_uuid_and_normalizes_at_compare_time():
    pipeline_src = Path("pcc/py_frontend/pipeline.py").read_text(encoding="utf-8")
    bootstrap_src = Path("scripts/bootstrap.sh").read_text(encoding="utf-8")

    assert "-Wl,-no_uuid" not in pipeline_src
    assert "pcc.macho_normalize" in bootstrap_src
