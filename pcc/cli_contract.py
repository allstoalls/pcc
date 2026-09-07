"""Self-host-safe source of truth for pcc CLI surface contracts.

The three entrypoints have different jobs: ``cli_core`` is the full host
owner, ``cli_bootstrap`` is the pcc1-safe Python subset, and ``pcc.py`` is the
legacy Click adapter. Shared values live here; intentional surface differences
are recorded explicitly instead of being inferred from three parsers.
"""

SURFACE_HOST = "cli_core"
SURFACE_BOOTSTRAP = "cli_bootstrap"
SURFACE_LEGACY_CLICK = "pcc.py"
ALL_CLI_SURFACES = (SURFACE_HOST, SURFACE_BOOTSTRAP, SURFACE_LEGACY_CLICK)

BACKEND_CHOICES = ("llvm", "llvm_capi", "self")
PYTHON_LIBPYTHON_CHOICES = ("auto", "on", "off")
IR_SCAFFOLD_CHOICES = ("auto", "on", "off")
DIAGNOSTIC_FORMAT_CHOICES = ("text", "json", "sarif")
DEFAULT_EMIT_LL = "__PCC_DEFAULT_LL__"
DEFAULT_PUBLIC_BACKEND = "self"

# Options consuming a following value before the source path. Used only for
# dispatch; the owning parser still validates each option and its value.
_INPUT_VALUE_OPTIONS = (
    "-o", "--backend", "--python-libpython", "--ir-scaffold",
    "--diagnostic-format", "--profile-json", "--pass", "--disable-pass",
    "--gpu-backend", "--target", "--emit-obj", "--emit-asm", "--jobs",
    "--cache-dir", "--sources-from-make", "--depends-on", "--cpp-arg",
    "--link-arg", "--prepare-cmd", "--ensure-make-goal", "--include-dir",
    "-I", "-D", "-U",
)


def cli_input_path(argv: list[str]) -> str:
    """Locate the input without confusing option values/program arguments."""
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if arg in _INPUT_VALUE_OPTIONS:
            index += 2
            continue
        if arg == "--emit-llvm":
            if index + 1 < len(argv):
                candidate = argv[index + 1]
                if not candidate.startswith("-") and (
                    candidate.endswith(".ll") or candidate.endswith(".bc")
                    or index + 2 < len(argv)
                ):
                    index += 2
                    continue
            index += 1
            continue
        if not arg.startswith("-"):
            return arg
        index += 1
    return ""

# (logical name, public flag, consuming surfaces)
SHARED_CLI_OPTIONS = (
    ("backend", "--backend", ALL_CLI_SURFACES),
    ("python_libpython", "--python-libpython", ALL_CLI_SURFACES),
    ("python_library", "--python-library", ALL_CLI_SURFACES),
    ("emit_llvm", "--emit-llvm", ALL_CLI_SURFACES),
    ("output", "-o", ALL_CLI_SURFACES),
    ("verbose", "--verbose", ALL_CLI_SURFACES),
)

# (feature group, present surfaces, absent surfaces, reason)
INTENDED_CLI_DIVERGENCES = (
    (
        "ir_scaffold",
        (SURFACE_HOST, SURFACE_BOOTSTRAP),
        (SURFACE_LEGACY_CLICK,),
        "pcc1/host Python lowering control; legacy Click remains C-oriented",
    ),
    (
        "diagnostic_profile_fallback_observability",
        (SURFACE_HOST, SURFACE_BOOTSTRAP),
        (SURFACE_LEGACY_CLICK,),
        "plain/self-host parsers own structured compiler observability",
    ),
    (
        "python_module_runner",
        (SURFACE_HOST, SURFACE_BOOTSTRAP),
        (SURFACE_LEGACY_CLICK,),
        "host safe-module and pcc1 native-module dispatch are distinct owners",
    ),
    (
        "gpu_backend",
        (SURFACE_HOST, SURFACE_LEGACY_CLICK),
        (SURFACE_BOOTSTRAP,),
        "device annotation compilation is not in the bootstrap CLI subset",
    ),
    (
        "c_project_build_options",
        (SURFACE_HOST, SURFACE_LEGACY_CLICK),
        (SURFACE_BOOTSTRAP,),
        "bootstrap delegates C/project inputs to the host CLI",
    ),
    (
        "pcc1_pytest",
        (SURFACE_BOOTSTRAP,),
        (SURFACE_HOST, SURFACE_LEGACY_CLICK),
        "compiled-stage native pytest subset is a bootstrap-only operation",
    ),
)


def validate_cli_contract() -> tuple[str, ...]:
    errors = []
    names = []
    flags = []
    for name, flag, surfaces in SHARED_CLI_OPTIONS:
        if name in names:
            errors.append("duplicate shared CLI option name: " + name)
        if flag in flags:
            errors.append("duplicate shared CLI flag: " + flag)
        names.append(name)
        flags.append(flag)
        if surfaces != ALL_CLI_SURFACES:
            errors.append("shared CLI option lacks all consumers: " + name)

    divergence_names = []
    for name, present, absent, reason in INTENDED_CLI_DIVERGENCES:
        if name in divergence_names:
            errors.append("duplicate CLI divergence: " + name)
        divergence_names.append(name)
        if not reason:
            errors.append("CLI divergence lacks reason: " + name)
        covered = []
        for surface in present + absent:
            if surface not in ALL_CLI_SURFACES:
                errors.append("unknown CLI surface in divergence: " + surface)
            if surface not in covered:
                covered.append(surface)
        if len(covered) != len(ALL_CLI_SURFACES):
            errors.append("CLI divergence does not classify every surface: " + name)
    return tuple(errors)


__all__ = [
    "ALL_CLI_SURFACES",
    "BACKEND_CHOICES",
    "DEFAULT_EMIT_LL",
    "DEFAULT_PUBLIC_BACKEND",
    "cli_input_path",
    "DIAGNOSTIC_FORMAT_CHOICES",
    "INTENDED_CLI_DIVERGENCES",
    "IR_SCAFFOLD_CHOICES",
    "PYTHON_LIBPYTHON_CHOICES",
    "SHARED_CLI_OPTIONS",
    "SURFACE_BOOTSTRAP",
    "SURFACE_HOST",
    "SURFACE_LEGACY_CLICK",
    "validate_cli_contract",
]
