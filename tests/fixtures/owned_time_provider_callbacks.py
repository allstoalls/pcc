import asyncio
import time
from time import monotonic_ns as imported_monotonic_ns
from time import perf_counter_ns as imported_perf_counter_ns
from time import time_ns as imported_time_ns


def main() -> None:
    loop = asyncio.new_event_loop()
    print("LOOP_CLOCK_ENTER")
    assert loop.time() >= 0.0
    loop.close()
    for clock in (time.time, time.monotonic, time.perf_counter):
        first = clock()
        second = clock()
        assert isinstance(first, float)
        assert second >= first
    assert time.time_ns() > 0 and imported_time_ns() > 0
    assert time.monotonic_ns() > 0 and imported_monotonic_ns() > 0
    assert time.perf_counter_ns() > 0 and imported_perf_counter_ns() > 0
    for clock in (time.time_ns, imported_time_ns, time.monotonic_ns,
                  imported_monotonic_ns, time.perf_counter_ns, imported_perf_counter_ns):
        value = clock()
        assert type(value) is int and value > 0
    for clock in (time.monotonic_ns, imported_monotonic_ns,
                  time.perf_counter_ns, imported_perf_counter_ns):
        first = clock()
        assert clock() >= first
    formatter = time.strftime
    assert formatter("owned-clock") == "owned-clock"
    sleeper = time.sleep
    assert sleeper(0) is None
    try:
        sleeper(-1)
    except ValueError as error:
        assert str(error) == "sleep length must be non-negative"
    else:
        raise AssertionError("negative delay was accepted")
    print("OWNED_TIME_OK")


main()
