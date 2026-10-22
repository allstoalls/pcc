"""Stdlib-only Linux/Windows process-tree supervisor for qualification runs.

This is a host test driver, not a tool invoked by native pcc. It owns only
children it starts and retains durable output and resource samples.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


WINDOWS_GUARD_SCHEMA = "pcc.windows-direct-root-guard.v1"
WINDOWS_GUARD_ENV = "PCC_WINDOWS_DIRECT_ROOT_GUARD"


def _direct_host_launch(command, environment, *, native):
    argv = list(command)
    child_env = dict(environment)
    if not native and sys.platform == "win32" and sys.implementation.name == "cpython":
        base = str(getattr(sys, "_base_executable", "") or "")
        executable = sys.executable
        if (argv and base and os.path.normcase(base) != os.path.normcase(executable)
                and os.path.normcase(argv[0]) == os.path.normcase(executable)):
            # The same CPython spawn protocol as worker_process_pool: avoid
            # a venv redirector PID while retaining sys.executable, sys.prefix
            # and the original environment's installed dependencies.
            argv = [base, *argv[1:]]
            child_env["__PYVENV_LAUNCHER__"] = executable
    return argv, child_env


def _windows_creation_time(handle):
    """Read the exact FILETIME identity of an already open process."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.GetProcessTimes
    query.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    query.restype = w.BOOL
    values = [w.FILETIME() for _ in range(4)]
    if not query(handle, *(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def _publish_windows_root_guard(path, process, limit, deadline):
    # Called after assignment and before resume: the child cannot observe a
    # receipt for a launch that has not entered the actual enforcing Job.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    receipt = {
        "schema": WINDOWS_GUARD_SCHEMA,
        "root_pid": process.pid,
        "root_creation_time": _windows_creation_time(int(process._handle)),
        "supervisor_pid": os.getpid(),
        "supervisor_creation_time": _windows_creation_time(kernel.GetCurrentProcess()),
        "cap_bytes": limit,
        "deadline": deadline,
    }
    temporary = str(path) + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


class WindowsJob:
    def __init__(self, limit):
        from ctypes import wintypes as w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        class BASIC(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("basic", BASIC), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.ntdll.NtResumeProcess.argtypes = [w.HANDLE]
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED()
        limits.basic.flags = 0x2200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
        limits.job_memory = limit
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def attach(self, process, before_resume=None):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            raise ctypes.WinError(ctypes.get_last_error())
        if before_resume is not None:
            before_resume()
        if self.ntdll.NtResumeProcess(int(process._handle)) < 0:
            self.kill()
            raise RuntimeError("NtResumeProcess failed")

    def sample(self):
        from ctypes import wintypes as w
        capacity = 4096
        buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
        if not self.kernel.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None):
            raise ctypes.WinError(ctypes.get_last_error())
        count = ctypes.c_uint32.from_buffer(buffer, 4).value
        if count > capacity:
            raise RuntimeError("job PID inventory exceeds watchdog capacity")
        class MEMORY(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("faults", w.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peak_ws", "working_set", "peak_paged", "paged",
                                                   "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
        rows = []
        for index in range(count):
            pid = ctypes.c_size_t.from_buffer(buffer, 8 + index * ctypes.sizeof(ctypes.c_size_t)).value
            handle = self.kernel.OpenProcess(0x410, False, pid)
            if not handle:
                continue
            try:
                memory = MEMORY()
                memory.size = ctypes.sizeof(memory)
                if not self.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.size):
                    continue
                name = ctypes.create_unicode_buffer(32768)
                length = w.DWORD(len(name))
                self.kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length))
                rows.append((pid, memory.working_set, name.value))
            finally:
                self.kernel.CloseHandle(handle)
        return rows

    def kill(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 124)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def linux_sample(root, known):
    records = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = {}
            for line in (entry / "status").read_text().splitlines():
                key, _, value = line.partition(":")
                fields[key] = value.strip()
            start = (entry / "stat").read_text().rsplit(")", 1)[1].split()[19]
            records[int(entry.name)] = (int(fields["PPid"]), int(fields.get("VmRSS", "0").split()[0]) * 1024, start)
        except (OSError, KeyError, ValueError):
            continue
    owned = {pid for pid, start in known.items() if pid in records and records[pid][2] == start}
    if root in records and (root not in known or records[root][2] == known[root]):
        owned.add(root)
    changed = True
    while changed:
        changed = False
        for pid, (parent, rss, start) in records.items():
            if parent in owned and pid not in owned:
                owned.add(pid)
                changed = True
    for pid in owned:
        known[pid] = records[pid][2]
    rows = []
    for pid in owned & set(records):
        try:
            executable = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            executable = ""
        rows.append((pid, records[pid][1], executable))
    return rows


def run(command, *, cwd, env, log_path, timeout, rss_limit, native=False, cancel=None):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        raise RuntimeError("qualification cancelled after a peer failure")
    child_env = dict(env)
    # Never inherit evidence for a different guard or a POSIX invocation.
    child_env.pop(WINDOWS_GUARD_ENV, None)
    guard_path = log_path.with_suffix(".windows-job.json")
    if os.name == "nt":
        child_env[WINDOWS_GUARD_ENV] = str(guard_path.absolute())
    launch_command, child_env = _direct_host_launch(command, child_env, native=native)
    known = {}
    peak = 0
    forbidden = {"python", "python3", "python3.15", "python.exe", "pythonw.exe",
                 "cc", "gcc", "clang", "clang.exe", "cl.exe", "ld", "ld.lld", "link.exe",
                 "ar", "ranlib", "make", "make.exe", "codesign",
                 "sh", "bash", "zsh", "cmd.exe", "powershell.exe", "pwsh.exe",
                 "ls", "rm", "mkdir", "find", "tar", "unzip", "nm", "objdump", "otool"}
    with log_path.open("wb") as log, log_path.with_suffix(".resources.jsonl").open("w") as samples:
        # Acquire the Job only after all pure preparation/file opens. Popen
        # failures must close it too; no suspended child may escape cleanup.
        job = WindowsJob(rss_limit) if os.name == "nt" else None
        process = None
        try:
            process = subprocess.Popen(launch_command, cwd=cwd, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=0x204 if job else 0,
                                       start_new_session=job is None)
            if job:
                job.attach(process, before_resume=lambda: _publish_windows_root_guard(
                    guard_path, process, rss_limit, started + timeout,
                ))
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("qualification cancelled after a peer failure")
                rows = job.sample() if job else linux_sample(process.pid, known)
                rss = sum(row[1] for row in rows)
                peak = max(peak, rss)
                samples.write(json.dumps({"elapsed": time.monotonic() - started, "rss": rss, "processes": rows}) + "\n")
                samples.flush()
                if native:
                    denied = [row[2] for row in rows if os.path.basename(row[2]).lower() in forbidden]
                    if denied:
                        raise RuntimeError("native stage invoked external execution owners: " + repr(denied))
                if rss > rss_limit:
                    raise RuntimeError("process-tree RSS cap exceeded")
                if time.monotonic() - started > timeout:
                    raise TimeoutError("stage deadline exceeded")
                time.sleep(0.2)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            try:
                if job:
                    try:
                        job.kill()
                    finally:
                        job.close()
                elif process is not None:
                    live_owned = linux_sample(process.pid, known)
                    for pid, _rss, _image in live_owned:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            finally:
                if process is not None:
                    process.wait(timeout=10)
    return {"seconds": time.monotonic() - started, "peak_tree_rss": peak,
            "log": str(log_path), "command": list(command), "launched_command": launch_command}
