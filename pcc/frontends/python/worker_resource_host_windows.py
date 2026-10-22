"""CPython-only Windows worker RSS I/O; never part of the native closure."""

import os
import struct


def _open_worker_resource_fd(path, _flags):
    """Return one noninheritable read fd that permits atomic replacement.

    Before open_osfhandle succeeds we own the HANDLE; afterward the fd owns it.
    This callback is used only by builtin open, which owns the returned fd and
    closes it on stream-construction failure or normal context-manager exit.
    """
    import ctypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int32
    # GENERIC_READ; FILE_SHARE_READ | WRITE | DELETE; NULL security attributes
    # (noninheritable); OPEN_EXISTING; FILE_ATTRIBUTE_NORMAL; no template.
    handle = create_file(path, 0x80000000, 0x00000007, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        # Preserve the conversion exception even if best-effort cleanup fails.
        try:
            close_handle(handle)
        except BaseException:
            pass
        raise


def open_worker_resource(path):
    return open(path, "r", encoding="utf-8", opener=_open_worker_resource_fd)


def _worker_resource_rename_information(path, pointer_bytes):
    """Serialize FILE_RENAME_INFO's Flags union, aligned HANDLE and UTF-16 tail.

    The Windows SDK layout is Flags@0, RootDirectory@8, FileNameLength@16,
    FileName@20 on 64-bit, and 0/4/8/12 on 32-bit. The structure sizes include
    the first WCHAR and tail padding: 24 and 16 bytes, respectively. Four
    extra zero bytes therefore keep the buffer >= sizeof(struct) + name bytes.
    """
    if "\0" in path:
        raise ValueError("worker RSS destination contains a null character")
    filename = path.encode("utf-16-le", "surrogatepass")
    if pointer_bytes == 8:
        header = struct.pack("<I4xQI", 3, 0, len(filename))
    elif pointer_bytes == 4:
        header = struct.pack("<III", 3, 0, len(filename))
    else:
        raise ValueError("unsupported Windows pointer size for worker RSS publication")
    return header + filename + b"\0" * 4


def replace_worker_resource(temporary, path):
    """Atomically publish only this RSS destination's closed .tmp file.

    No delete/rename sequence or ordinary MoveFileEx fallback can preserve an
    open old snapshot. Unsupported extended rename fails without weakening the
    publication contract. The caller owns the existing bounded retry/cleanup.
    """
    import ctypes

    if "\0" in temporary or "\0" in path:
        raise ValueError("worker RSS publication path contains a null character")
    destination = os.path.abspath(path)
    source = os.path.abspath(temporary)
    if source != destination + ".tmp":
        raise ValueError("worker RSS publication requires its same-directory .tmp")
    information = _worker_resource_rename_information(destination, struct.calcsize("P"))
    buffer = ctypes.create_string_buffer(information)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32]
    rename.restype = ctypes.c_int32
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int32
    invalid_handle = ctypes.c_void_p(-1).value
    # DELETE access; all three share modes; NULL security attributes makes the
    # handle noninheritable. Reopen only the already-closed temporary snapshot.
    handle = create_file(source, 0x00010000, 7, None, 3, 0x80, None)
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    rename_result = None
    try:
        # FileRenameInfoEx=22; information.Flags=REPLACE_IF_EXISTS|POSIX=3.
        rename_result = rename(handle, 22, buffer, len(information))
        if not rename_result:
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException as error:
        # Only an explicit BOOL=0 proves that publication did not commit. An
        # interrupted FFI call may already have changed the namespace, so keep
        # that outcome unknown and do not retry it as ordinary 5/32 contention.
        committed = None if rename_result is None else bool(rename_result)
        failure = error
        if committed is not False:
            message = ("worker RSS rename outcome unknown after API interruption"
                       if committed is None else
                       "worker RSS rename committed but API return handling failed")
            failure = RuntimeError(message)
        failure.publication_committed = committed
        failure.handle_close_attempted = True
        failure.handle_close_confirmed = False
        try:
            if close_handle(handle):
                failure.handle_close_confirmed = True
            else:
                failure.handle_cleanup_error = ctypes.WinError(ctypes.get_last_error())
        except BaseException as cleanup_error:
            failure.handle_cleanup_error = cleanup_error
        if failure is not error:
            raise failure from error
        raise
    # The rename is already committed. A cleanup failure must not masquerade
    # as pre-commit sharing contention or trigger another rename attempt.
    try:
        closed = close_handle(handle)
        if not closed:
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException as error:
        failure = RuntimeError("worker RSS rename committed but handle cleanup failed")
        failure.publication_committed = True
        failure.handle_close_attempted = True
        failure.handle_close_confirmed = False
        raise failure from error
