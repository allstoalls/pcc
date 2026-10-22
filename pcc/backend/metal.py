from __future__ import annotations

"""Small Metal toolchain hooks for future pcc GPU kernels."""

import ctypes
from pathlib import Path
import subprocess
import tempfile


class MetalToolchainUnavailable(RuntimeError):
    """Raised when Xcode's Metal toolchain cannot be found."""


class MetalCompileError(RuntimeError):
    """Raised when the Metal compiler rejects generated source."""


def _looks_like_missing_metal_toolchain(message: str) -> bool:
    text = message.lower()
    return (
        "missing metal toolchain" in text
        or "cannot execute tool 'metal'" in text
        or "xcodebuild -downloadcomponent metaltoolchain" in text
        or "unable to find utility \"metal\"" in text
        or "unable to find utility 'metal'" in text
    )


def find_metal_compiler(
    *,
    xcrun: str = "/usr/bin/xcrun",
    timeout: float = 10.0,
) -> str | None:
    try:
        result = subprocess.run(
            [xcrun, "--find", "metal"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path or None


def metal_toolchain_usable(
    *,
    xcrun: str = "/usr/bin/xcrun",
    timeout: float = 10.0,
) -> bool:
    """Return true only when the Metal compiler can actually execute.

    ``xcrun --find metal`` can succeed on machines where Xcode still reports a
    missing Metal Toolchain component. Treat that as unavailable for claim and
    test-gate purposes.
    """
    if find_metal_compiler(xcrun=xcrun, timeout=timeout) is None:
        return False
    try:
        result = subprocess.run(
            [xcrun, "-sdk", "macosx", "metal", "-v"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def compile_metal_source_to_air(
    source: str,
    output_path: str | Path,
    *,
    xcrun: str = "/usr/bin/xcrun",
    timeout: float = 30.0,
) -> Path:
    metal = find_metal_compiler(xcrun=xcrun)
    if metal is None:
        raise MetalToolchainUnavailable(
            "Metal compiler not found; install Xcode Metal Toolchain"
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pcc_metal_") as tmp:
        src = Path(tmp) / "kernel.metal"
        src.write_text(source, encoding="utf-8")
        try:
            result = subprocess.run(
                [xcrun, "metal", "-c", str(src), "-o", str(out)],
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise MetalCompileError(
                f"Metal compiler timed out after {timeout:g}s"
            ) from exc
        except FileNotFoundError as exc:
            raise MetalToolchainUnavailable(
                "xcrun not found; install Xcode command line tools"
            ) from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        if _looks_like_missing_metal_toolchain(message):
            raise MetalToolchainUnavailable(message)
        raise MetalCompileError(message or "Metal compiler failed")
    if not out.is_file():
        raise MetalCompileError(f"Metal compiler produced no output: {out}")
    return out


def compile_air_to_metallib(
    air_paths: list[str | Path],
    output_path: str | Path,
    *,
    xcrun: str = "/usr/bin/xcrun",
    timeout: float = 30.0,
) -> Path:
    if find_metal_compiler(xcrun=xcrun) is None:
        raise MetalToolchainUnavailable(
            "Metal compiler not found; install Xcode Metal Toolchain"
        )
    if not air_paths:
        raise MetalCompileError("no Metal AIR inputs supplied")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [xcrun, "metallib"]
    cmd.extend(str(Path(path)) for path in air_paths)
    cmd.extend(["-o", str(out)])
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MetalCompileError(
            f"metallib timed out after {timeout:g}s"
        ) from exc
    except FileNotFoundError as exc:
        raise MetalToolchainUnavailable(
            "xcrun not found; install Xcode command line tools"
        ) from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        if _looks_like_missing_metal_toolchain(message):
            raise MetalToolchainUnavailable(message)
        raise MetalCompileError(message or "metallib failed")
    if not out.is_file():
        raise MetalCompileError(f"metallib produced no output: {out}")
    return out


def compile_metal_runtime_bridge(
    output_path: str | Path,
    *,
    source_path: str | Path | None = None,
    xcrun: str = "/usr/bin/xcrun",
    timeout: float = 30.0,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if source_path is None:
        source_path = Path(__file__).with_name("gpu_metal_runtime.m")
    source = Path(source_path)
    cmd = [
        xcrun,
        "clang",
        "-fobjc-arc",
        "-c",
        str(source),
        "-o",
        str(out),
    ]
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MetalCompileError(
            f"Metal runtime bridge compile timed out after {timeout:g}s"
        ) from exc
    except FileNotFoundError as exc:
        raise MetalToolchainUnavailable(
            "xcrun not found; install Xcode command line tools"
        ) from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise MetalCompileError(message or "Metal runtime bridge compile failed")
    if not out.is_file():
        raise MetalCompileError(f"Metal runtime bridge produced no output: {out}")
    return out


def link_metal_runtime_bridge_dylib(
    output_path: str | Path,
    *,
    object_path: str | Path,
    xcrun: str = "/usr/bin/xcrun",
    timeout: float = 30.0,
) -> Path:
    """Link a compiled Objective-C Metal bridge object into a loadable dylib."""
    obj = Path(object_path)
    if not obj.is_file():
        raise MetalCompileError(f"Metal runtime bridge object not found: {obj}")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        xcrun,
        "clang",
        "-dynamiclib",
        str(obj),
        "-framework",
        "Foundation",
        "-framework",
        "Metal",
        "-o",
        str(out),
    ]
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MetalCompileError(
            f"Metal runtime bridge dylib link timed out after {timeout:g}s"
        ) from exc
    except FileNotFoundError as exc:
        raise MetalToolchainUnavailable(
            "xcrun not found; install Xcode command line tools"
        ) from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise MetalCompileError(message or "Metal runtime bridge dylib link failed")
    if not out.is_file():
        raise MetalCompileError(f"Metal runtime bridge dylib produced no output: {out}")
    return out


def validate_dynamic_library_symbol(
    library_path: str | Path,
    symbol: str,
) -> str:
    """Load a dynamic library and verify that *symbol* is exported.

    This performs ``dlopen``/``dlsym`` only. It does not call the symbol.
    """
    lib_path = Path(library_path)
    if not lib_path.is_file():
        raise MetalCompileError(f"dynamic library not found: {lib_path}")
    if not symbol:
        raise MetalCompileError("dynamic library symbol name is empty")
    try:
        lib = ctypes.CDLL(str(lib_path))
    except OSError as exc:
        raise MetalCompileError(f"dynamic library load failed: {exc}") from exc
    try:
        getattr(lib, symbol)
    except AttributeError as exc:
        raise MetalCompileError(f"dynamic library symbol not found: {symbol}") from exc
    return symbol
