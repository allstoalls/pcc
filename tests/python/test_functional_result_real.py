from pcc.library.functional_result import Result, fuse_map_filter


def test_result_map_bind():
    assert Result.Ok(1).map(lambda x: x + 1).value == 2
    assert Result.Ok(1).bind(lambda x: Result.Ok(x + 2)).value == 3
    assert Result.Err("bad").map(lambda x: x + 1).value == "bad"


def test_fuse_map_filter():
    assert fuse_map_filter([1, 2, 3], lambda x: x * 2, lambda x: x > 2) == [4, 6]
