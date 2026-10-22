"""Render the pcc architecture with pyncd's compositional diagram model.

This is documentation tooling, not a pcc compiler dependency.  The diagram
uses pyncd's wires as *artifact labels*, rather than tensor dimensions.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import construction_helpers  # noqa: F401 -- registers pyncd's @ and * operators
import data_structure.Category as cat
import data_structure.Operators as ops
import websocket_transfer.auxiliary_information as annotations
import websocket_transfer.headless as headless
import websocket_transfer.send_morphism as sending
import websocket_transfer.standalone_page as standalone


HERE = Path(__file__).resolve().parent


def stage(label: str, signature: str):
    return ops.GenericOperator.template(label, signature)


def group(title: str, body, color: str, description: str):
    return cat.Block.template(
        body,
        title=r"\text{" + title + "}",
        description=description,
        fill_color=color,
    )


def architecture():
    """One selected CPU frontend, owned emission, and the runtime link input."""
    c_frontend = group(
        "C frontend",
        stage("CPP", "src -> tokens")
        @ stage("ParseC", "tokens -> cast")
        @ stage("LowerC", "cast -> ir"),
        "#d7e8f5",
        "C preprocessing, parsing and semantic lowering: "
        "pcc/frontends/c/evaluator/c_evaluator.py, pcc/frontends/c/parse/, pcc/frontends/c/codegen/c_codegen.py. "
        "Some current routes still use external owners; the owned C path is a contract.",
    )
    python_frontend = group(
        "Python frontend",
        stage("ParsePy", "src -> pyast")
        @ stage("Infer", "pyast -> typed")
        @ stage("LowerPy", "typed -> ir"),
        "#e5ddf4",
        "Python parsing, type inference and native lowering: "
        "pcc/frontends/python/py_parse.py, pcc/frontends/python/type_infer.py, "
        "pcc/frontends/python/codegen/.",
    )
    frontends = group(
        "Source-mode alternatives",
        stage("Dispatch", "src -> src,src")
        @ (c_frontend * python_frontend)
        @ stage("Select", "ir,ir -> ir"),
        "#eee8d7",
        "The driver selects one route for an input. The two displayed lanes "
        "are alternatives, not two frontends executed for every file. "
        "Host and native entrypoints have a parity contract; check actual gaps "
        "in pcc/driver/cli_core.py and pcc/driver/cli_bootstrap.py.",
    )
    compiler = group(
        "Compiler and owned CPU backend",
        stage("Entry", "src -> src")
        @ frontends
        @ group(
            "IR passes",
            stage("Passes", "ir -> optir"),
            "#d8e8df",
            "Python pass dispatch is in pcc/frontends/python/pipeline_pass_driver.py; "
            "C pass orchestration is in pcc/frontends/c/evaluator/c_evaluator.py. "
            "Effective pass selection depends on mode and options.",
        )
        @ group(
            "Self backend",
            stage("Emit", "optir -> obj"),
            "#d9e5f0",
            "pcc/backend/ owns machine lowering and native object emission. "
            "The public default is self (pcc/driver/cli_contract.py). "
            "LLVM and LLVM-CAPI remain explicit reference or migration paths.",
        ),
        "#e9edf4",
        "Public entrypoints: pcc, python -m pcc, native pcc1 and CPython "
        "import pcc APIs. "
        "The figure shows their shared intended semantics, not proof that "
        "all current paths have converged.",
    )
    runtime = group(
        "Python runtime archive",
        stage("Semantics", "rtsrc -> rtsem")
        @ stage("GC5", "rtsem -> gc")
        @ stage("Archive", "gc -> rtobj"),
        "#d9eadf",
        "pcc/runtime/py/ is the production migration target; "
        "pcc/runtime/src/ is a C transition implementation and oracle. "
        "The five selectable backends share slot/root tracing and update "
        "contracts: refcount-cycle, incremental-tricolor, concurrent-mark-sweep, "
        "generational-minor-major and colored-relocating.",
    )
    linked = group(
        "Native CPU execution",
        (compiler * runtime)
        @ stage("Link", "obj,rtobj -> exe")
        @ stage("Run", "exe -> result"),
        "#e9edf4",
        "Self-backend output is linked with the runtime archive for Python "
        "programs. Native emitted execution is the behavior proof. "
        "Optional libpython mode and the bounded Metal kernel path have "
        "separate ownership and validation boundaries.",
    )
    build_pcc3 = stage("pcc2", "stage2 -> stage3")
    keep_pcc2 = cat.ProdObject((build_pcc3.dom()[0],)).identity()
    bootstrap = group(
        "Self-host chain",
        stage("pcc0", "host -> stage1")
        @ stage("pcc1", "stage1 -> stage2")
        @ stage("Fork", "stage2 -> stage2,stage2")
        @ (keep_pcc2 * build_pcc3)
        @ stage("Compare", "stage2,stage3 -> fixed"),
        "#e8e2d4",
        "The host-built pcc1 must build pcc2, and pcc2 must build pcc3. "
        "Only stable pcc2/pcc3 output establishes a fixed point; a stage1 "
        "build alone does not. Every result must label host/native, "
        "self/LLVM, and no-libpython/libpython modes.",
    )
    return group(
        "pcc architecture",
        linked * bootstrap,
        "#e9edf4",
        "The upper circuit is CPU compilation and runtime linkage. "
        "The lower circuit is its independent self-host qualification chain. "
        "Wires name artifacts; this is an architecture map, not a tensor model.",
    )


async def render(dist: Path, output: Path) -> None:
    model = architecture()
    settings = sending.display_settings(
        darkMode=True,
        debugBorders=False,
        width=1500,
        subBlocks=True,
        inspectionBoxes=True,
        title="pcc architecture | compiler, runtime, self host",
    )
    standalone.save_standalone_page(
        model,
        output.with_suffix(".html"),
        settings=settings,
        auxiliary=annotations.auxiliary_information(model, with_legend=False),
        dist=dist,
    )
    await headless.save_figures(
        {
            output.with_suffix(".svg").name: model,
            output.with_suffix(".png").name: model,
        },
        directory=output.parent,
        dist=dist,
        width=1500,
        darkMode=True,
        debugBorders=False,
        subBlocks=True,
        scale=1.5,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tsncd-dist", type=Path, required=True)
    parser.add_argument(
        "--output-base",
        type=Path,
        default=HERE / "pcc-pyncd-architecture",
    )
    args = parser.parse_args()
    asyncio.run(render(args.tsncd_dist, args.output_base.resolve()))


if __name__ == "__main__":
    main()
