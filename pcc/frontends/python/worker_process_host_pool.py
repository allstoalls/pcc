"""Host process-pool implementations, excluded from native import closure.

The public module supplies its current dependency objects and follow-up helper
at each call so its existing host monkeypatch seams remain usable. No import
back into that module is needed.
"""


def host_pool(specs, width, *, os, subprocess, sys, time):
    """CPython's standard process API implements the host platform boundary."""
    active = []
    next_index = 0

    def signal_group(process, number):
        process.poll()
        try:
            if sys.platform == "win32":
                if process.returncode is None:
                    process.kill() if number == 9 else process.terminate()
            else:
                os.killpg(process.pid, number)
        except ProcessLookupError:
            pass
        except PermissionError:
            # The leader may exit between the pre-signal poll and Darwin's
            # orphan/zombie-group EPERM. Reap that race before treating the
            # group as a live worker we could not stop.
            process.poll()
            # Darwin reports EPERM for an orphan group containing only
            # zombies. Its leader has already been reaped in this case.
            if process.returncode is None:
                raise

    try:
        while next_index < len(specs) or active:
            pending = []
            for index, process in active:
                result = process.poll()
                if result is None:
                    pending.append((index, process))
                else:
                    signal_group(process, 9)
                    if result:
                        return ((index + 1) << 32) | (result & 4294967295)
            active = pending
            while next_index < len(specs) and len(active) < width:
                argv, env_vector = specs[next_index]
                env = dict(item.split("=", 1) for item in env_vector)
                try:
                    process = (subprocess.Popen(argv, env=env, creationflags=512)
                               if sys.platform == "win32"
                               else subprocess.Popen(argv, env=env, process_group=0))
                except OSError:
                    return ((next_index + 1) << 32) | 127
                active.append((next_index, process))
                next_index += 1
            if active:
                time.sleep(0.01)
    finally:
        if active:
            for _index, process in active:
                signal_group(process, 15)
            time.sleep(0.2)
            for _index, process in active:
                signal_group(process, 9)
                process.wait(timeout=2)
    return 0


def host_weighted_pool(
    specs, reservations, width, budget, *, os, subprocess, sys, time,
):
    active = []
    pending = list(range(len(specs)))
    available = budget

    def signal_group(process, number):
        process.poll()
        try:
            if sys.platform == "win32":
                if process.returncode is None:
                    process.kill() if number == 9 else process.terminate()
            else:
                os.killpg(process.pid, number)
        except ProcessLookupError:
            pass
        except PermissionError:
            # The leader may exit between the pre-signal poll and Darwin's
            # orphan/zombie-group EPERM. Reap that race before treating the
            # group as a live worker we could not stop.
            process.poll()
            if process.returncode is None:
                raise

    try:
        while pending or active:
            survivors = []
            for index, process in active:
                result = process.poll()
                if result is None:
                    survivors.append((index, process))
                    continue
                signal_group(process, 9)
                available += reservations[index]
                if result:
                    return ((index + 1) << 32) | (result & 4294967295)
            active = survivors
            while pending and len(active) < width:
                selected = next(
                    (index for index in pending
                     if reservations[index] <= available or not active),
                    None,
                )
                if selected is None:
                    break
                pending.remove(selected)
                argv, env_vector = specs[selected]
                env = dict(item.split("=", 1) for item in env_vector)
                try:
                    process = (subprocess.Popen(argv, env=env, creationflags=512)
                               if sys.platform == "win32"
                               else subprocess.Popen(argv, env=env, process_group=0))
                except OSError:
                    return ((selected + 1) << 32) | 127
                active.append((selected, process))
                available -= reservations[selected]
            if active:
                time.sleep(0.01)
    finally:
        for _index, process in active:
            signal_group(process, 15)
        if active:
            time.sleep(0.2)
        for _index, process in active:
            signal_group(process, 9)
            process.wait(timeout=2)
    return 0


def host_chained_pool(
    specs, reservations, followups, followup_paths, floor, width, budget, *,
    os, subprocess, sys, time, followup_reservation,
):
    count = len(specs)
    weights = list(reservations) + [0] * count
    ready = list(range(count))
    followup_ready = []
    active = []
    available = budget
    completed = 0

    def signal_group(process, number):
        process.poll()
        try:
            if sys.platform == "win32":
                if process.returncode is None:
                    process.kill() if number == 9 else process.terminate()
            else:
                os.killpg(process.pid, number)
        except ProcessLookupError:
            pass
        except PermissionError:
            # The leader may exit between the pre-signal poll and Darwin's
            # orphan/zombie-group EPERM. Reap that race before treating the
            # group as a live worker we could not stop.
            process.poll()
            if process.returncode is None:
                raise

    try:
        while completed < 2 * count:
            survivors = []
            for index, process in active:
                result = process.poll()
                if result is None:
                    survivors.append((index, process))
                    continue
                signal_group(process, 9)
                available += weights[index]
                completed += 1
                if result:
                    return ((index + 1) << 32) | (result & 4294967295)
                if index < count:
                    try:
                        weights[count + index] = followup_reservation(
                            followup_paths[index], floor,
                        )
                    except OSError:
                        return ((count + index + 1) << 32) | 127
                    followup_ready.append(count + index)
            active = survivors
            while len(active) < width:
                # Same order as the native pool: the next fitting primary,
                # then the lowest-index ready follow-up that fits.
                selected = next(
                    (index for index in ready
                     if weights[index] <= available or not active),
                    None,
                )
                if selected is None:
                    selected = next(
                        (index for index in sorted(followup_ready)
                         if weights[index] <= available or not active),
                        None,
                    )
                if selected is None:
                    break
                if selected < count:
                    ready.remove(selected)
                else:
                    followup_ready.remove(selected)
                spec = specs[selected] if selected < count else followups[selected - count]
                argv, env_vector = spec
                env = dict(item.split("=", 1) for item in env_vector)
                try:
                    process = (subprocess.Popen(argv, env=env, creationflags=512)
                               if sys.platform == "win32"
                               else subprocess.Popen(argv, env=env, process_group=0))
                except OSError:
                    return ((selected + 1) << 32) | 127
                active.append((selected, process))
                available -= weights[selected]
            if active:
                time.sleep(0.01)
    finally:
        for _index, process in active:
            signal_group(process, 15)
        if active:
            time.sleep(0.2)
        for _index, process in active:
            signal_group(process, 9)
            process.wait(timeout=2)
    return 0
