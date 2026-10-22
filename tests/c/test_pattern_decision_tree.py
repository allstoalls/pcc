from pcc.library.pattern_decision_tree import PatternCase, build_decision_tree


def test_decision_tree_deduplicates():
    nodes = build_decision_tree(
        [PatternCase("A", "a"), PatternCase("A", "b")],
        default="d",
    )
    assert [node.tag for node in nodes] == ["A", "_"]
