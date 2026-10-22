def maybe_fail(fail: bool) -> float:
    local = ["float-owned-local"]
    if fail:
        raise ValueError("floating sentinel")
    return 1.25


def main() -> None:
    assert maybe_fail(False) == 1.25
    try:
        maybe_fail(True)
    except ValueError as error:
        assert str(error) == "floating sentinel"
        print("FLOAT_CAUGHT")
    else:
        raise AssertionError("floating error was lost")
    assert maybe_fail(False) == 1.25
    function = maybe_fail
    try:
        function(True)
    except ValueError as error:
        assert str(error) == "floating sentinel"
        print("ADAPTER_FLOAT_CAUGHT")
    else:
        raise AssertionError("adapter floating error was lost")
    assert function(False) == 1.25


main()
