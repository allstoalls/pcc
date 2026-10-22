#!/usr/bin/env python3
"""Run a command inside the Linux x86_64 self-backend container.

Python port of ``scripts/run_self_backend_linux_x86_64_docker.sh``: builds the
image when needed, mounts the checkout at ``/workspace``, forwards an optional
host artifact directory, and execs ``docker run ... "$@"``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "self-backend-linux-x86_64.Dockerfile"
DEFAULT_IMAGE = "pcc-self-backend-linux-x86_64:latest"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]

    image = os.environ.get("PCC_SELF_BACKEND_LINUX_X86_64_IMAGE") or DEFAULT_IMAGE
    docker = shutil.which("docker")
    if not docker:
        print(
            "docker is required for the Linux x86_64 self-backend harness",
            file=sys.stderr,
        )
        return 127
    if not DOCKERFILE.is_file():
        print(f"missing Dockerfile: {DOCKERFILE}", file=sys.stderr)
        return 1

    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    rebuild = os.environ.get("PCC_SELF_BACKEND_DOCKER_REBUILD", "0") == "1"
    if not rebuild:
        inspected = subprocess.run(
            [docker, "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        rebuild = inspected.returncode != 0
    if rebuild:
        built = subprocess.run(
            [
                docker, "build",
                "--platform", "linux/amd64",
                "-f", str(DOCKERFILE),
                "-t", image,
                str(ROOT),
            ],
            env=environment,
            check=False,
        )
        if built.returncode != 0:
            return built.returncode

    docker_args = [
        docker, "run", "--rm",
        "--platform", "linux/amd64",
        "-e", "UV_PROJECT_ENVIRONMENT=/tmp/pcc-linux-x86_64-venv",
        "-e", "UV_LINK_MODE=copy",
        "-e", "PCC_BUILD_SKIP=1",
    ]
    host_artifacts = os.environ.get("PCC_SELF_BACKEND_HOST_ARTIFACTS") or ""
    if host_artifacts:
        if not os.path.isabs(host_artifacts):
            print(
                "PCC_SELF_BACKEND_HOST_ARTIFACTS must be an absolute path",
                file=sys.stderr,
            )
            return 2
        Path(host_artifacts).mkdir(parents=True, exist_ok=True)
        docker_args += [
            "-e", "PCC_HOST_TEST_ARTIFACTS=/pcc-host-test-artifacts",
            "-v", f"{host_artifacts}:/pcc-host-test-artifacts",
        ]
    docker_args += ["-v", f"{ROOT}:/workspace", "-w", "/workspace", image]
    docker_args += command
    return subprocess.run(docker_args, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
