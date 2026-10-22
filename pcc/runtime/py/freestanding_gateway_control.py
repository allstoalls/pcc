"""Async-signal-safe process control owned by freestanding pcc-Python.

The installed native handlers perform one lock-free i32 store and return.  No
managed object, allocator, scheduler, TLS provider or Python callback is
entered from signal context.  ``pcc.gateway.control`` polls and clears the
flags from ordinary execution before it performs reload or graceful drain.

This module uses the named POSIX ``sigaction`` ABI.  It is therefore a Darwin
libSystem / Linux libc platform boundary and is not part of the Linux zero-libc
claim.  Gateway TLS already names an external OpenSSL/libc boundary; plaintext
deployments that require zero-libc need a separately gated raw-syscall signal
owner before enabling this component.

The strict freestanding ABI does not currently admit ``sigprocmask`` or
``pthread_sigmask``.  Blocking these signals only in the installing thread
would not protect already-running carrier threads in any case.  The lifecycle
contract is therefore deliberately narrower: install before carrier threads
start, and uninstall only after they have quiesced.  Every partial
``sigaction`` transition is tracked below.  A failed restore keeps ownership
and pending flags fail-closed instead of exposing a mixed disposition set as a
successful uninstall.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int, c_ptr, extern
from pcc.unsafe import (
    atomic_cas_i32,
    atomic_rmw_i32,
    atomic_store_i32,
    define_global_i32,
    define_global_i64_array,
    function_addr,
    global_addr,
    int_to_ptr,
    memset,
    null,
    ptr_add,
    stack_alloc,
    store_ptr,
)


__pcc_freestanding__ = True


_sigaction = extern("sigaction", (c_int, c_ptr, c_ptr), c_int)
_sigemptyset = extern("sigemptyset", (c_ptr,), c_int)

define_global_i32("pcc_gateway_control_installed", 0)
define_global_i32("pcc_gateway_control_action_mask", 0)
define_global_i32("pcc_gateway_control_stop_requested", 0)
define_global_i32("pcc_gateway_control_reload_requested", 0)
define_global_i64_array(
    "pcc_gateway_control_old_hup",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_i64_array(
    "pcc_gateway_control_old_int",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_i64_array(
    "pcc_gateway_control_old_pipe",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_i64_array(
    "pcc_gateway_control_old_term",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)


@c_abi_export("pcc_gateway_control_signal_handler")
def _gateway_control_signal_handler(signal_number: i64) -> None:
    """The complete async handler: one lock-free global store."""

    if signal_number == 1:  # SIGHUP on both supported POSIX targets.
        atomic_store_i32(
            global_addr("pcc_gateway_control_reload_requested"),
            0,
            1,
            "release",
        )
    elif signal_number == 2 or signal_number == 15:  # SIGINT / SIGTERM.
        atomic_store_i32(
            global_addr("pcc_gateway_control_stop_requested"),
            0,
            1,
            "release",
        )


@c_abi_export("pcc_gateway_control_set_action")
def _set_action(signal_number: i64, handler, old_action) -> i64:
    action = stack_alloc(160)
    memset(action, 0, 160)
    store_ptr(action, 0, handler)
    # The repository's POSIX signal port uses the same conservative 160-byte
    # allocation and mask-at-eight contract on supported Darwin/Linux ABIs.
    if _sigemptyset(ptr_add(action, 8)) != 0:
        return -1
    if _sigaction(signal_number, action, old_action) != 0:
        return -1
    return 0


@c_abi_export("pcc_gateway_control_restore_action")
def _restore_action(signal_number: i64, old_action) -> i64:
    return _sigaction(signal_number, old_action, null())


@c_abi_export("pcc_gateway_control_restore_modified_actions")
def _restore_modified_actions() -> i64:
    """Try every saved restore and retain a mask of dispositions still owned."""

    action_mask = global_addr("pcc_gateway_control_action_mask")
    remaining = atomic_rmw_i32("or", action_mask, 0, 0, "acquire")
    if remaining & 8:
        if _restore_action(
            15, global_addr("pcc_gateway_control_old_term")
        ) == 0:
            remaining = remaining & 7
    if remaining & 4:
        if _restore_action(
            2, global_addr("pcc_gateway_control_old_int")
        ) == 0:
            remaining = remaining & 11
    if remaining & 2:
        if _restore_action(
            1, global_addr("pcc_gateway_control_old_hup")
        ) == 0:
            remaining = remaining & 13
    if remaining & 1:
        if _restore_action(
            13, global_addr("pcc_gateway_control_old_pipe")
        ) == 0:
            remaining = remaining & 14
    atomic_store_i32(action_mask, 0, remaining, "release")
    return remaining


@c_abi_export("pcc_gateway_control_failed_install")
def _failed_install() -> i64:
    """Roll back an install transaction without discarding failed restores."""

    installed = global_addr("pcc_gateway_control_installed")
    if _restore_modified_actions() != 0:
        # State 3 is a poisoned partial install.  No positive capability was
        # returned, but the process-wide owner remains reserved.  A later
        # install call may retry the saved restores before installing anew.
        atomic_store_i32(installed, 0, 3, "release")
        return -4
    atomic_store_i32(
        global_addr("pcc_gateway_control_stop_requested"), 0, 0, "release"
    )
    atomic_store_i32(
        global_addr("pcc_gateway_control_reload_requested"), 0, 0, "release"
    )
    atomic_store_i32(installed, 0, 0, "release")
    return -3


@c_abi_export("pcc_gateway_control_v1_install")
def pcc_gateway_control_v1_install() -> i64:
    """Own SIGINT/SIGTERM/SIGHUP and ignore SIGPIPE process-wide.

    Return a positive process capability on success and ``-1`` when another
    gateway control owner is already installed.  ``-3`` means the platform
    operation failed and rollback completed.  ``-4`` means at least one prior
    disposition could not be restored; ownership and pending flags remain
    reserved until a later install call can finish recovery.
    """

    installed = global_addr("pcc_gateway_control_installed")
    prior_state = atomic_cas_i32(
        installed, 0, 0, 4, "acq_rel", "acquire"
    )
    if prior_state == 3:
        # Claim the poisoned transaction exclusively and retry every saved
        # restore before any old-action buffer can be overwritten.
        if atomic_cas_i32(
            installed, 0, 3, 4, "acq_rel", "acquire"
        ) != 3:
            return -1
        if _restore_modified_actions() != 0:
            atomic_store_i32(installed, 0, 3, "release")
            return -4
    elif prior_state != 0:
        return -1
    # Whether this is a fresh install or a successful poisoned-transaction
    # recovery, notifications observed by the previous handler generation do
    # not belong to the new owner capability.
    atomic_store_i32(
        global_addr("pcc_gateway_control_stop_requested"),
        0,
        0,
        "release",
    )
    atomic_store_i32(
        global_addr("pcc_gateway_control_reload_requested"),
        0,
        0,
        "release",
    )
    action_mask = global_addr("pcc_gateway_control_action_mask")
    atomic_store_i32(action_mask, 0, 0, "release")
    handler = function_addr("pcc_gateway_control_signal_handler")
    old_hup = global_addr("pcc_gateway_control_old_hup")
    old_int = global_addr("pcc_gateway_control_old_int")
    old_pipe = global_addr("pcc_gateway_control_old_pipe")
    old_term = global_addr("pcc_gateway_control_old_term")
    if _set_action(13, int_to_ptr(1), old_pipe) != 0:  # SIGPIPE
        return _failed_install()
    atomic_store_i32(action_mask, 0, 1, "release")
    if _set_action(1, handler, old_hup) != 0:  # SIGHUP
        return _failed_install()
    atomic_store_i32(action_mask, 0, 3, "release")
    if _set_action(2, handler, old_int) != 0:  # SIGINT
        return _failed_install()
    atomic_store_i32(action_mask, 0, 7, "release")
    if _set_action(15, handler, old_term) != 0:  # SIGTERM
        return _failed_install()
    atomic_store_i32(action_mask, 0, 15, "release")
    atomic_store_i32(installed, 0, 1, "release")
    return 1


@c_abi_export("pcc_gateway_control_v1_poll")
def pcc_gateway_control_v1_poll(owner: i64) -> i64:
    """Atomically consume stop/reload flags as bit 0/bit 1."""

    if owner != 1:
        return -2
    if atomic_rmw_i32(
        "or",
        global_addr("pcc_gateway_control_installed"),
        0,
        0,
        "acquire",
    ) != 1:
        # In particular, do not consume pending flags while an uninstall
        # restore is incomplete (state 2).
        return -4
    stop = atomic_rmw_i32(
        "xchg",
        global_addr("pcc_gateway_control_stop_requested"),
        0,
        0,
        "acq_rel",
    )
    reload = atomic_rmw_i32(
        "xchg",
        global_addr("pcc_gateway_control_reload_requested"),
        0,
        0,
        "acq_rel",
    )
    return (stop & 1) | ((reload & 1) << 1)


@c_abi_export("pcc_gateway_control_v1_uninstall")
def pcc_gateway_control_v1_uninstall(owner: i64) -> i64:
    """Restore all prior dispositions and release process-wide ownership.

    Restore failure returns ``-4`` and leaves state 2, the residual action
    mask, owner capability, and pending flags intact.  The same owner may call
    uninstall again to retry only the remaining dispositions.
    """

    if owner != 1:
        return -2
    installed = global_addr("pcc_gateway_control_installed")
    prior_state = atomic_cas_i32(
        installed, 0, 1, 5, "acq_rel", "acquire"
    )
    if prior_state != 1:
        if prior_state != 2:
            return -2
        if atomic_cas_i32(
            installed, 0, 2, 5, "acq_rel", "acquire"
        ) != 2:
            return -2
    if _restore_modified_actions() != 0:
        atomic_store_i32(installed, 0, 2, "release")
        return -4
    atomic_store_i32(
        global_addr("pcc_gateway_control_stop_requested"), 0, 0, "release"
    )
    atomic_store_i32(
        global_addr("pcc_gateway_control_reload_requested"), 0, 0, "release"
    )
    atomic_store_i32(installed, 0, 0, "release")
    return 0
