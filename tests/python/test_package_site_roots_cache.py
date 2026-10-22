"""package_site_roots must not re-resolve the package environment per call.

The stage2 coordinator profile attributed 34% of its sampled window to
resolve_pcc_native_extension_path, whose every call re-ran
resolve_package_environment (13 env reads + environment.json open/read/parse)
via package_site_roots.  The roots are a pure function of the environment
fingerprint; the per-call filesystem PROBES (isdir/isfile/listdir) stay live
so freshly installed packages are still found within one process.
"""
from __future__ import annotations

import pytest

from pcc.package import environment as package_environment
from pcc.frontends.python import pipeline_packages


def test_site_roots_resolve_once_per_environment(monkeypatch, tmp_path):
    calls = {"n": 0}
    real = pipeline_packages.environment_site_roots

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(pipeline_packages, "environment_site_roots", counting)
    pipeline_packages._SITE_ROOTS_CACHE.clear()

    first = pipeline_packages.package_site_roots()
    again = pipeline_packages.package_site_roots()
    assert again == first
    assert calls["n"] == 1

    # A changed environment fingerprint must re-resolve, not serve stale
    # roots (test isolation: monkeypatched sites between compiles).
    site = tmp_path / "fresh-site"
    site.mkdir()
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))
    changed = pipeline_packages.package_site_roots()
    assert calls["n"] == 2
    assert str(site) in changed


def test_environment_fingerprint_tracks_inputs(monkeypatch):
    base = package_environment.package_environment_fingerprint()
    monkeypatch.setenv("PCC_PACKAGE_SITE", "/nonexistent/site-a")
    changed = package_environment.package_environment_fingerprint()
    assert changed != base
    monkeypatch.setenv("PCC_PACKAGE_SITE", "/nonexistent/site-b")
    assert package_environment.package_environment_fingerprint() != changed


def test_cached_roots_still_probe_the_filesystem(monkeypatch, tmp_path):
    # The isdir filter runs per call: a site dir created after the first
    # call becomes visible without an environment change... by design the
    # RESOLUTION is cached, existence filtering is not.
    site = tmp_path / "lazy-site"
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))
    pipeline_packages._SITE_ROOTS_CACHE.clear()
    before = pipeline_packages.package_site_roots()
    assert str(site) not in before
    site.mkdir()
    after = pipeline_packages.package_site_roots()
    assert str(site) in after
