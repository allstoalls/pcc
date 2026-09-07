from __future__ import annotations


def test_public_pcc_launcher_uses_shared_bootstrap_dispatch(monkeypatch):
    import pcc.cli_bootstrap as cli_bootstrap
    import pcc.cli_launcher as launcher

    calls = []

    def fake_cli_main(argv):
        calls.append(list(argv))
        return 17

    monkeypatch.setattr(cli_bootstrap, "bootstrap_cli_main", fake_cli_main)

    assert launcher.main(["pcc/__main__.py", "-o", "out"]) == 17
    assert calls == [["pcc/__main__.py", "-o", "out"]]
