from pcc.support.compiler_hot_objects import HotObjectCandidate, rank_hot_objects, recommend_slots


def test_hot_object_ranking():
    ranked = rank_hot_objects([
        HotObjectCandidate("a", 1, 1, "slots"),
        HotObjectCandidate("b", 10, 10, "struct"),
    ])
    assert ranked[0].name == "b"


def test_slots_recommendation():
    assert recommend_slots(HotObjectCandidate("Node", 1000, 1, "slots"))
