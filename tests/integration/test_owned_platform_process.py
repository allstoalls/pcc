"""Native process boundaries, using only programs emitted by the supplied pcc1.

The child is the emitted executable itself; no shell, host Python, or external
utility participates. The Windows cases additionally inspect actual handle
inheritance and the process handle count after successful and failed spawns.
"""

import os

import pytest

from tests.integration.test_owned_platform_native import compile_run, native_compiler

pytestmark = pytest.mark.integration


def test_owned_process_argv_environment_and_suppression(native_compiler, tmp_path):
    compile_run(native_compiler, tmp_path / "directory with spaces 中文", r'''
        #include <string.h>
        extern long long pcc_platform_spawnp(char **, char **, long long);
        extern long long pcc_platform_waitpid(long long, void *, long long);
        extern long long py_process_normalize_wait_status(long long);
        extern char *pcc_platform_getenv(char *);
        extern long long pcc_platform_write(long long, void *, long long);
        int main(int argc, char **argv) {
            if (argc > 1) {
                if (argc != 5) return 31;
                if (strcmp(argv[1], "child")) return 32;
                if (strcmp(argv[2], "")) return 33;
                if (strcmp(argv[3], "two words \"quote\" \\ trailing\\")) return 34;
                if (strcmp(argv[4], "中文")) return 35;
                char *only = pcc_platform_getenv("PCC_CHILD_ONLY");
                if (!only || strcmp(only, "present")) return 36;
                if (pcc_platform_getenv("PCC_PARENT_ONLY")) return 37;
                pcc_platform_write(1, "hidden-out", 10);
                pcc_platform_write(2, "hidden-err", 10);
                return 23;
            }
            char *args[] = {argv[0], "child", "", "two words \"quote\" \\ trailing\\", "中文", 0};
            char *env[] = {"PCC_CHILD_ONLY=present", 0};
            long long pid = pcc_platform_spawnp(args, env, 1);
            if (pid <= 0) return 1;
            int status = 0;
            if (pcc_platform_waitpid(pid, &status, 0) != pid) return 2;
            if (py_process_normalize_wait_status(status) != 23) return 3;
            if (pcc_platform_waitpid(pid, &status, 0) >= 0) return 4;
            pcc_platform_write(1, "process-ok\n", 11);
            return 0;
        }
    ''', suffix=".c", expected="process-ok\n", extra_env={"PCC_PARENT_ONLY": "hidden"})


# Windows handle inheritance boundary
@pytest.mark.pcc_gate(probe=lambda: not (os.name != "nt"))
def test_windows_process_pipe_eof_and_handle_cleanup(native_compiler, tmp_path):
    compile_run(native_compiler, tmp_path, r'''
        #include <string.h>
        extern void *GetCurrentProcess(void);
        extern int GetProcessHandleCount(void *, void *);
        extern int GetHandleInformation(void *, void *);
        extern int CreatePipe(void *, void *, void *, int);
        extern int SetHandleInformation(void *, int, int);
        extern int CloseHandle(void *);
        extern long long pcc_win_spawn_process(char *, char **, char **, long long);
        extern long long pcc_win_spawn_process_pipe(char *, char **, char **, long long, void *);
        extern long long pcc_platform_waitpid(long long, void *, long long);
        extern long long pcc_platform_read(long long, void *, long long);
        extern long long pcc_platform_write(long long, void *, long long);
        extern long long pcc_platform_close(long long);
        static void number(char *out, unsigned long long value) {
            char reverse[32];
            int n = 0;
            do { reverse[n++] = '0' + value % 10; value /= 10; } while (value);
            int i = 0;
            while (n) out[i++] = reverse[--n];
            out[i] = 0;
        }
        static unsigned long long parse(char *in) {
            unsigned long long value = 0;
            while (*in) { value = value * 10 + *in - '0'; ++in; }
            return value;
        }
        int main(int argc, char **argv) {
            if (argc > 1) {
                int flags = 0;
                if (GetHandleInformation((void *)parse(argv[1]), &flags)) return 41;
                if (pcc_platform_write(1, "pipe-data", 9) != 9) return 42;
                return 0;
            }
            unsigned int before = 0, after = 0;
            if (!GetProcessHandleCount(GetCurrentProcess(), &before)) return 1;
            void *reader = 0, *writer = 0;
            if (!CreatePipe(&reader, &writer, 0, 0)) return 2;
            if (!SetHandleInformation(writer, 1, 1)) return 3;
            char inherited[32];
            number(inherited, (unsigned long long)writer);
            char *args[] = {argv[0], inherited, 0};
            char *env[] = {0};
            int fd = -1;
            long long pid = pcc_win_spawn_process_pipe(argv[0], args, env, 1, &fd);
            if (pid <= 0) return 4;
            char buffer[16];
            int total = 0;
            for (;;) {
                long long n = pcc_platform_read(fd, buffer + total, 16 - total);
                if (n < 0) return 5;
                if (!n) break;
                total += n;
                if (total == 16) return 6;
            }
            if (total != 9 || memcmp(buffer, "pipe-data", 9)) return 7;
            pcc_platform_close(fd);
            int status = 0;
            if (pcc_platform_waitpid(pid, &status, 0) != pid || status) return 8;
            CloseHandle(reader);
            CloseHandle(writer);
            for (int i = 0; i < 32; ++i) {
                if (pcc_win_spawn_process("Z:\\pcc-no-such-file-7f6e.exe", args, env, 1) >= 0) return 9;
                if (pcc_win_spawn_process_pipe("Z:\\pcc-no-such-file-7f6e.exe", args, env, 1, &fd) >= 0) return 10;
            }
            if (!GetProcessHandleCount(GetCurrentProcess(), &after)) return 11;
            if (after != before) return 12;
            pcc_platform_write(1, "handles-ok\n", 11);
            return 0;
        }
    ''', suffix=".c", expected="handles-ok\n")


def test_subprocess_direct_argv_and_replaced_environment(native_compiler, tmp_path):
    compile_run(native_compiler, tmp_path, r'''
        import os
        import subprocess
        import sys
        def run():
            if len(sys.argv) > 1:
                assert sys.argv[1] == ""
                assert sys.argv[2] == "a quote \" and a trailing\\"
                assert sys.argv[3] == "中文"
                if os.getenv("PCC_CHILD_ONLY") == "present":
                    assert os.getenv("PCC_PARENT_ONLY") is None
                print("child-output")
                return
            command = [sys.executable, "", "a quote \" and a trailing\\", "中文"]
            result = subprocess.run(command, env={"PCC_CHILD_ONLY": "present"}, capture_output=True)
            assert result.returncode == 0
            output = subprocess.check_output(command)
            assert output == b"child-output\n"
            print("subprocess-ok")
        run()
    ''', expected="subprocess-ok\n", extra_env={"PCC_PARENT_ONLY": "hidden"})


# Linux raw descriptor collision
@pytest.mark.pcc_gate(probe=lambda: not (os.name == "nt"))
def test_linux_capture_with_original_stdout_and_stderr_closed(native_compiler, tmp_path):
    compile_run(native_compiler, tmp_path, r'''
        extern long long pcc_platform_spawnp(char **, char **, long long);
        extern long long pcc_platform_waitpid(long long, void *, long long);
        extern long long py_process_normalize_wait_status(long long);
        extern long long pcc_platform_write(long long, void *, long long);
        extern long long pcc_platform_close(long long);
        int main(int argc, char **argv) {
            if (argc > 1) {
                if (pcc_platform_write(1, "a", 1) != 1) return 31;
                if (pcc_platform_write(2, "b", 1) != 1) return 32;
                return 0;
            }
            pcc_platform_close(1);
            pcc_platform_close(2);
            char *args[] = {argv[0], "child", 0};
            char *env[] = {0};
            long long pid = pcc_platform_spawnp(args, env, 1);
            if (pid <= 0) return 1;
            int status = 0;
            if (pcc_platform_waitpid(pid, &status, 0) != pid) return 2;
            return py_process_normalize_wait_status(status);
        }
    ''', suffix=".c")


# Linux pipe2/dup2 descriptor collision
@pytest.mark.pcc_gate(probe=lambda: not (os.name == "nt"))
def test_linux_pipe_reuses_closed_standard_descriptors(native_compiler, tmp_path):
    compile_run(native_compiler, tmp_path, r'''
        import subprocess
        import sys
        from pcc.unsafe import close
        def run():
            if len(sys.argv) > 1:
                print("pipe-child")
                return
            close(0)
            close(1)
            result = subprocess.check_output([sys.executable, "child"])
            assert result == b"pipe-child\n"
        run()
    ''')
