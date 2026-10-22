"""Wheel build hook — produce libpy_runtime_pcc_py.a + native pcc1 binary.

The hook fires at wheel-build time (`python -m build`,
`pip install .`, or `pip install python-cc` going through sdist):

1. Run pcc itself (CPython-hosted) to compile pcc/runtime — both
   the C runtime sources and the pcc-Python runtime ports — into
   ``libpy_runtime_pcc_py.a``.
2. Run pcc again to self-compile ``pcc/__main__.py`` into a native
   ``pcc1`` executable, mirroring ``scripts/bootstrap.py`` stage1.
3. Bundle both native artifacts into the wheel: the archive plus its verified
   provenance/C-API-inventory sidecars under ``pcc/runtime`` (consumed by
   the lazy first-run path), and the binary at ``.data/scripts/pcc1`` as the
   explicit native bootstrap helper.

The default ``self`` backend owns native artifact construction. A failure is
fatal; the hook never retries a different backend.

Honours environment overrides:
- PCC_BUILD_BACKEND=self          select the owned backend (default: self)
- PCC_BUILD_TARGET=<archive>      override the output archive name
- PCC_RUNTIME_BUILD=make          explicit CPython-host reference oracle only
- PCC_BUILD_PCC1=<path>           bundle an already verified platform pcc1
                                  instead of rebuilding it (release/CI reuse)
- PCC_BUILD_SKIP=1                skip both runtime + binary build
                                  (dev iteration only)

Archive and binary failures are both fatal for a wheel. A wheel-installed
native ``pcc1`` must not depend on rebuilding runtime objects in the user's
environment. The Python ``pcc`` launcher exists, but published sdists should
still prove both native artifacts can be built.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import subprocess
import sys
from types import ModuleType
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _load_runtime_archive_provenance():
    """Load the verifier owned by this source artifact.

    Hatch imports custom hooks in an isolated build environment before the
    project is installed, so the checkout is not an import root.  Importing
    ``pcc.tools`` here would either fail or, worse, select an unrelated already
    installed ``pcc``. Load the verifier and its owned archive reader in a
    private source package, without changing ``sys.path`` or importing the
    public pcc package.
    """

    provenance_path = (
        Path(__file__).resolve().parent
        / "pcc"
        / "tools"
        / "runtime_archive_provenance.py"
    )
    if not provenance_path.is_file():
        raise RuntimeError(
            f"runtime archive provenance verifier is missing: {provenance_path}"
        )
    source_package = provenance_path.parents[1]
    archive_path = source_package / "backend" / "ar.py"
    if not archive_path.is_file():
        raise RuntimeError(f"runtime archive reader is missing: {archive_path}")
    namespace = "_pcc_wheel_source_" + hashlib.sha256(
        str(source_package).encode("utf-8")
    ).hexdigest()[:16]
    for suffix, directory in (("", source_package), (".backend", archive_path.parent),
                              (".tools", provenance_path.parent)):
        package = ModuleType(namespace + suffix)
        package.__path__ = [str(directory)]
        sys.modules[package.__name__] = package
    archive_spec = importlib.util.spec_from_file_location(
        namespace + ".backend.ar", archive_path
    )
    if archive_spec is None or archive_spec.loader is None:
        raise RuntimeError(f"cannot load runtime archive reader: {archive_path}")
    archive_reader = importlib.util.module_from_spec(archive_spec)
    sys.modules[archive_spec.name] = archive_reader
    archive_spec.loader.exec_module(archive_reader)
    provenance_spec = importlib.util.spec_from_file_location(
        namespace + ".tools.runtime_archive_provenance",
        provenance_path,
    )
    if provenance_spec is None or provenance_spec.loader is None:
        raise RuntimeError(
            f"cannot load runtime archive provenance verifier: {provenance_path}"
        )
    provenance = importlib.util.module_from_spec(provenance_spec)
    sys.modules[provenance_spec.name] = provenance
    provenance_spec.loader.exec_module(provenance)
    return provenance


_provenance = _load_runtime_archive_provenance()
ProvenanceError = _provenance.ProvenanceError
capi_inventory_path_for_archive = _provenance.capi_inventory_path_for_archive
manifest_path_for_archive = _provenance.manifest_path_for_archive
verify_runtime_archive_manifest = _provenance.verify_runtime_archive_manifest


def _load_build_targets():
    path = Path(__file__).resolve().parent / "pcc" / "frontends" / "python" / "pipeline_targets.py"
    spec = importlib.util.spec_from_file_location("_pcc_build_targets", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load owned target definitions: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_build_targets = _load_build_targets()


def _native_wheel_tag() -> str:
    """``py3-none-<platform>``: native artifacts, but no CPython extension.

    pcc1 and the runtime archive do not depend on the building interpreter, so
    one wheel serves every supported Python. The platform part follows pcc1:
    the owned Mach-O linker writes minos 12.0, and the owned ELF output is a
    static executable that needs no particular glibc.
    """
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "py3-none-macosx_12_0_arm64"
        if machine in ("x86_64", "amd64"):
            return "py3-none-macosx_12_0_x86_64"
    elif sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "py3-none-manylinux_2_17_x86_64"
        if machine in ("aarch64", "arm64"):
            return "py3-none-manylinux_2_17_aarch64"
    elif sys.platform == "win32":
        if machine in ("amd64", "x86_64"):
            return "py3-none-win_amd64"
    raise RuntimeError(
        f"no native wheel tag for platform {sys.platform!r} machine {machine!r}"
    )


class CustomBuildHook(BuildHookInterface):
    """Hatchling hook: build the pcc-Python runtime archive + native pcc1."""

    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        if version == "editable":
            self.app.display_info(
                "editable build: skipping release-only runtime archive and pcc1 build"
            )
            return
        if os.environ.get("PCC_BUILD_SKIP") == "1":
            self.app.display_info("PCC_BUILD_SKIP=1, skipping runtime + binary build")
            return
        if getattr(self, "target_name", "") == "sdist":
            # The sdist carries sources only. The wheel build constructs the
            # runtime archive and bundles pcc1; an sdist build that tried both
            # hit the 10-minute self-compile budget and failed.
            self.app.display_info("sdist build: skipping runtime archive and pcc1 build")
            return

        root = Path(self.root)
        runtime_dir = root / "pcc" / "runtime"
        target = os.environ.get("PCC_BUILD_TARGET", "libpy_runtime_pcc_py.a")
        archive = runtime_dir / target

        if not runtime_dir.is_dir():
            self.app.display_warning(
                f"runtime dir {runtime_dir} not present in source tree; "
                "skipping pre-build (lazy first-run will handle it)"
            )
            return

        backend = os.environ.get("PCC_BUILD_BACKEND", "self")

        # Cross-arch guard: `make` trusts mtimes only, so an archive
        # built on another platform/arch (e.g. a host Darwin archive
        # leaking into a Linux container through a bind mount) looks
        # "fresh" and gets linked — GNU ld then silently skips the
        # wrong-arch members and every runtime symbol goes undefined.
        # The lazy pipeline path already stamps archives with a target
        # id; honour the same stamp here and force a rebuild on
        # mismatch. (docs/investigations/linux-x86-64-docker-harness-rot.md No.5)
        self._discard_wrong_target_archives(runtime_dir)

        # ---- 1. runtime archive (required for the wheel) ----
        force_runtime_rebuild = self._runtime_archive_inputs_newer(root, archive)
        ok = self._run_make(
            runtime_dir,
            target,
            backend,
            force=force_runtime_rebuild,
        )
        if ok and archive.is_file():
            manifest = self._require_runtime_archive_manifest(archive)
            capi_inventory = capi_inventory_path_for_archive(archive)
            self._write_archive_target_stamp(archive)
            rel = f"pcc/runtime/{target}"
            build_data.setdefault("force_include", {})[str(archive)] = rel
            build_data.setdefault("force_include", {})[str(manifest)] = (
                rel + ".provenance.json"
            )
            build_data.setdefault("force_include", {})[str(capi_inventory)] = (
                rel + ".capi_syms"
            )
            stamp = Path(str(archive) + ".target")
            if not stamp.is_file():
                raise RuntimeError(
                    f"runtime archive target stamp was not created: {stamp}"
                )
            build_data.setdefault("force_include", {})[str(stamp)] = rel + ".target"
            wheel_stamp = self._write_wheel_archive_stamp(archive)
            build_data.setdefault("force_include", {})[str(wheel_stamp)] = (
                rel + ".wheel"
            )
            self.app.display_info(f"bundled runtime archive: {rel}")
        else:
            raise RuntimeError(
                "pcc runtime archive build failed; refusing to publish a wheel "
                f"without pcc/runtime/{target}"
            )

        # ---- 2. native pcc1 binary (hard-fail for published sdists) ----
        prebuilt = str(os.environ.get("PCC_BUILD_PCC1", "") or "").strip()
        if prebuilt:
            out_binary = Path(prebuilt).expanduser().resolve()
            if not self._validate_prebuilt_pcc1(out_binary):
                raise RuntimeError(
                    f"PCC_BUILD_PCC1={out_binary} is not an executable pcc1 "
                    "for this build host"
                )
            self.app.display_info(f"reusing verified native pcc1: {out_binary}")
        else:
            out_binary = runtime_dir / "_native" / ("pcc1.exe" if os.name == "nt" else "pcc1")
            bin_backend = backend
            ok = self._run_pcc_self_compile(root, out_binary, bin_backend)
            if not ok:
                raise RuntimeError(
                    f"pcc self-compile failed under --backend={bin_backend}. "
                    "The published wheel requires a native `pcc1` helper. Inspect the warnings "
                    "above, or set PCC_BUILD_SKIP=1 for a dev iteration only."
                )

        build_data.setdefault("shared_scripts", {})[str(out_binary)] = "pcc1.exe" if os.name == "nt" else "pcc1"
        build_data["pure_python"] = False
        build_data["tag"] = _native_wheel_tag()
        self.app.display_info("bundled native pcc1 binary at .data/scripts/pcc1")

    def _validate_prebuilt_pcc1(self, path: Path) -> bool:
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        try:
            process = subprocess.run(
                [str(path), "--help"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return process.returncode == 0 and "Usage: pcc" in process.stdout

    def _archive_target_id(self) -> str:
        """Use the same owned host ABI as the compiler, including in isolation."""
        import platform

        triple = _build_targets.host_target_triple()
        machine = platform.machine().lower()
        if machine in ("amd64", "x64"):
            machine = "x86_64"
        return f"{sys.platform}:{machine}:{triple}"

    def _discard_wrong_target_archives(self, runtime_dir: Path) -> None:
        import shutil

        want = self._archive_target_id()
        mismatched = False
        for archive in runtime_dir.glob("libpy_runtime*.a"):
            stamp = Path(str(archive) + ".target")
            manifest = manifest_path_for_archive(archive)
            capi_inventory = capi_inventory_path_for_archive(archive)
            have = ""
            try:
                have = stamp.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if have == want:
                continue
            mismatched = True
            self.app.display_info(
                f"discarding {archive.name}: target stamp "
                f"{have or '<missing>'} != {want}"
            )
            try:
                archive.unlink()
            except OSError:
                pass
            try:
                stamp.unlink()
            except OSError:
                pass
            try:
                manifest.unlink()
            except OSError:
                pass
            try:
                capi_inventory.unlink()
            except OSError:
                pass
        if mismatched:
            # `make` would otherwise re-link the archive from the
            # wrong-arch .o files it sees as up to date (the lazy
            # pipeline path uses `make -B` for the same reason).
            for objdir in ("build", "build_pcc", "build_py", "build_libpython"):
                shutil.rmtree(runtime_dir / objdir, ignore_errors=True)

    def _write_archive_target_stamp(self, archive: Path) -> None:
        try:
            Path(str(archive) + ".target").write_text(
                self._archive_target_id() + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def _write_wheel_archive_stamp(self, archive: Path) -> Path:
        manifest = manifest_path_for_archive(archive)
        capi_inventory = capi_inventory_path_for_archive(archive)

        def file_sha256(path: Path) -> str:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()

        # Never write into self.directory: that is the distribution output
        # directory (dist/), and every non-distribution entry left there makes
        # the PyPI upload fail with "InvalidDistribution: Unknown distribution
        # format". The marker only needs a real path on disk because it is
        # force_include-d into the artifact under pcc/runtime/.
        marker_root = Path(self.root) / "build" / "pcc-runtime-wheel-markers"
        marker_root.mkdir(parents=True, exist_ok=True)
        marker = marker_root / (archive.name + ".wheel")
        marker.write_text(
            "pcc.runtime-wheel-artifact.v2\n"
            + "target="
            + self._archive_target_id()
            + "\narchive-sha256="
            + file_sha256(archive)
            + "\nmanifest-sha256="
            + file_sha256(manifest)
            + "\ncapi-inventory-sha256="
            + file_sha256(capi_inventory)
            + "\n",
            encoding="utf-8",
        )
        return marker

    def _runtime_archive_manifest_valid(self, archive: Path) -> bool:
        try:
            verify_runtime_archive_manifest(
                archive,
                runtime_root=archive.parent,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired, ProvenanceError):
            return False
        return True

    def _require_runtime_archive_manifest(self, archive: Path) -> Path:
        manifest = manifest_path_for_archive(archive)
        try:
            verify_runtime_archive_manifest(
                archive,
                runtime_root=archive.parent,
            )
        except (
            OSError,
            UnicodeError,
            subprocess.TimeoutExpired,
            ProvenanceError,
        ) as exc:
            raise RuntimeError(
                f"runtime archive provenance verification failed for {archive}: {exc}"
            ) from exc
        return manifest

    def _runtime_archive_inputs_newer(self, root: Path, archive: Path) -> bool:
        """Mirror the pipeline's pcc-emitted archive freshness contract."""
        if not archive.is_file():
            return True
        if not self._runtime_archive_manifest_valid(archive):
            return True
        archive_mtime = archive.stat().st_mtime
        pcc_root = root / "pcc"
        input_roots = [
            pcc_root / "backend",
            pcc_root / "frontends",
            pcc_root / "driver",
            pcc_root / "ir",
            pcc_root / "runtime",
            pcc_root / "tools",
            pcc_root / "__main__.py",
            pcc_root / "api.py",
            pcc_root / "pcc.py",
        ]
        ignored_dirs = {
            ".git",
            ".pytest_cache",
            "__pycache__",
            "build",
            "build_libpython",
            "build_pcc",
            "build_py",
        }
        pending = list(input_roots)
        while pending:
            current = pending.pop()
            if current.is_file():
                try:
                    if current.stat().st_mtime > archive_mtime:
                        return True
                except OSError:
                    return True
                continue
            if not current.is_dir():
                continue
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in ignored_dirs and not entry.name.startswith(
                        "."
                    ):
                        pending.append(entry)
                    continue
                if not (
                    entry.suffix in (".py", ".c", ".h") or entry.name == "Makefile"
                ):
                    continue
                try:
                    if entry.stat().st_mtime > archive_mtime:
                        return True
                except OSError:
                    return True
        return False

    def _run_make(
        self,
        runtime_dir: Path,
        target: str,
        backend: str,
        *,
        force: bool = False,
    ) -> bool:
        """Select a source/configuration-verified owned runtime for the wheel.

        The legacy method name remains for build-hook consumers. Make is an
        explicitly selected host reference; construction failures never change
        the selected owner or permit publication of an incomplete wheel.
        """
        if backend != "self":
            raise RuntimeError("wheel runtime construction requires PCC_BUILD_BACKEND=self")
        build_mode = str(os.environ.get("PCC_RUNTIME_BUILD", "") or "").strip().lower() or "owned"
        if build_mode not in ("owned", "make"):
            raise RuntimeError("invalid PCC_RUNTIME_BUILD; expected 'owned' or 'make'")
        if build_mode == "make":
            if sys.implementation.name != "cpython":
                raise RuntimeError("PCC_RUNTIME_BUILD=make is a CPython-host reference oracle; native pcc1 requires owned runtime construction")
            self.app.display_info("explicit host runtime reference oracle PCC_RUNTIME_BUILD=make (external Make/tool commands; not owned construction)")
        else:
            # Hatch imports the hook before this source package is installed.
            # Import the canonical selector in the source-root child rather
            # than accidentally importing an unrelated installed pcc here.
            # It admits the complete module inventory, threads/refcount,
            # target, source and codegen receipts, and reuses current builds.
            script = (
                "import os, shutil, sys\n"
                "from pcc.frontends.python.owned_runtime_build import ensure_target_runtime\n"
                "runtime_dir, archive, target = sys.argv[1:]\n"
                "selected = ensure_target_runtime(runtime_dir, target, packaged_archive=archive)\n"
                "if os.path.abspath(selected) != os.path.abspath(archive):\n"
                "    for suffix in ('.capi_syms', '', '.provenance.json'):\n"
                "        shutil.copyfile(selected + suffix, archive + suffix + '.wheel-tmp')\n"
                "        os.replace(archive + suffix + '.wheel-tmp', archive + suffix)\n"
            )
            environment = dict(os.environ)
            environment["PCC_RUNTIME_DIR"] = str(runtime_dir.resolve())
            environment.pop("PCC_RUNTIME_ARCHIVE", None)
            command = [sys.executable, "-c", script, str(runtime_dir.resolve()),
                       str((runtime_dir / target).resolve()), _build_targets.host_target_triple()]
            try:
                subprocess.run(command, cwd=self.root, check=True, capture_output=True,
                               text=True, timeout=2400, env=environment)
                return True
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                self.app.display_warning("owned runtime build failed: " + str(exc))
                return False
        env = dict(os.environ)
        # Inject the backend flag directly into the PCC command so
        # the Makefile doesn't need to learn a new variable. The
        # existing `$(PCC) --cpp-arg=... --emit-obj ...` invocations
        # in pcc/runtime/Makefile pick this up unchanged.
        env["PCC"] = f"{sys.executable} -m pcc --backend {backend}"

        cmd = ["make"]
        if force:
            cmd.append("-B")
        cmd.extend(["-C", str(runtime_dir), target])
        try:
            subprocess.run(
                cmd,
                check=True,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return True
        except FileNotFoundError:
            self.app.display_warning("make is not on PATH; cannot pre-build runtime")
            return False
        except subprocess.TimeoutExpired:
            self.app.display_warning("runtime build timed out after 10 minutes")
            return False
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or exc.stdout or "")[-2000:]
            self.app.display_warning(
                f"make {target} (backend={backend}) failed:\n{tail}"
            )
            return False

    def _run_pcc_self_compile(
        self,
        root: Path,
        out_binary: Path,
        backend: str,
    ) -> bool:
        """Compile pcc/__main__.py into a native pcc binary.

        Mirrors ``scripts/bootstrap.py`` stage1: CPython-hosted pcc
        compiles its own entry point with ``--backend self
        --python-libpython=off``. Returns True iff pcc exits 0 and the
        output file exists and is executable.
        """
        main_py = root / "pcc" / "__main__.py"
        if not main_py.is_file():
            self.app.display_warning(
                f"{main_py} not present in source tree; cannot self-compile"
            )
            return False

        out_binary.parent.mkdir(parents=True, exist_ok=True)
        if out_binary.exists():
            out_binary.unlink()

        env = dict(os.environ)
        env.setdefault("PCC_RUNTIME_CC", "pcc")
        env.setdefault("PCC_RUNTIME_HIGH", "py")

        cmd = [
            sys.executable,
            "-m",
            "pcc",
            "--backend",
            backend,
            "--python-libpython",
            "off",
            "--ir-scaffold=on",
            str(main_py),
            "-o",
            str(out_binary),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                env=env,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            self.app.display_warning(
                "python interpreter not on PATH; cannot self-compile"
            )
            return False
        except subprocess.TimeoutExpired:
            self.app.display_warning("pcc self-compile timed out after 10 minutes")
            return False
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or exc.stdout or "")[-2000:]
            self.app.display_warning(
                f"pcc self-compile (backend={backend}) failed:\n{tail}"
            )
            return False

        if not out_binary.is_file() or not os.access(out_binary, os.X_OK):
            self.app.display_warning(
                f"pcc self-compile claimed success but {out_binary} is "
                "missing or not executable"
            )
            return False
        return True
