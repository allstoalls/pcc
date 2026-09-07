"""``python -m pcc`` — the same command path as the installed ``pcc`` script.

Both reach one owner, :func:`pcc.cli_bootstrap.bootstrap_cli_main`; there is no
second argument parser here. The import is direct and at module scope because
the closed-world dependency walk anchors the stage1 closure on this file, and
routing it through :mod:`pcc.cli_launcher` instead collapsed that closure from
216 modules to 2.
"""
from pcc.cli_bootstrap import bootstrap_cli_sys_argv_exit


if __name__ == "__main__":
    bootstrap_cli_sys_argv_exit()
