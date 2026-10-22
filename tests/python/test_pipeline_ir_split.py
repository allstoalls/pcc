"""Shared IR sharding contracts behind the pipeline facade."""

from __future__ import annotations

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_ir_split


_MODULE = """@state = private global i64 0

define internal i64 @first() {
entry:
  %v = load i64, ptr @state
  ret i64 %v
}

define i64 @second() {
entry:
  %v = call i64 @first()
  ret i64 %v
}
"""


def test_pass_and_object_shards_rename_private_symbols_consistently():
    pass_shards = pipeline_ir_split.split_python_ir_module_for_pass_shards(
        _MODULE,
        export_prefix="__pass_",
        shard_bytes=1,
    )
    object_shards = (
        pipeline_ir_split.split_self_backend_ir_module_for_object_shards(
            _MODULE,
            export_prefix="__object_",
            shard_bytes=1,
        )
    )

    assert len(pass_shards) >= 2
    assert len(object_shards) >= 2
    assert any("@__pass_first" in shard for shard in pass_shards)
    assert any("@__object_first" in shard for shard in object_shards)


def test_pipeline_facade_reexports_both_ir_splitters():
    assert (
        pipeline._split_python_ir_module_for_pass_shards
        is pipeline_ir_split.split_python_ir_module_for_pass_shards
    )
    assert (
        pipeline._split_self_backend_ir_module_for_object_shards
        is pipeline_ir_split.split_self_backend_ir_module_for_object_shards
    )
