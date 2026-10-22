"""Facade contract for extracted self-backend host subprocess payloads."""

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_self_backend_host


def test_pipeline_reexports_self_backend_host_payloads_by_identity():
    assert (
        pipeline._SELF_BACKEND_HOST_CODE
        is pipeline_self_backend_host._SELF_BACKEND_HOST_CODE
    )
    assert (
        pipeline._COMPILER_CACHE_RETENTION_HOST_CODE
        is pipeline_self_backend_host._COMPILER_CACHE_RETENTION_HOST_CODE
    )
    assert (
        pipeline._SELF_BACKEND_HOST_MANY_CODE
        is pipeline_self_backend_host._SELF_BACKEND_HOST_MANY_CODE
    )
    assert (
        pipeline._SELF_BACKEND_OBJECT_CACHE_PLAN_CODE
        is pipeline_self_backend_host._SELF_BACKEND_OBJECT_CACHE_PLAN_CODE
    )
    assert (
        pipeline._SELF_BACKEND_OBJECT_CACHE_PUBLISH_CODE
        is pipeline_self_backend_host._SELF_BACKEND_OBJECT_CACHE_PUBLISH_CODE
    )


def test_extracted_payloads_keep_their_host_boundary_entrypoints():
    single = pipeline_self_backend_host._SELF_BACKEND_HOST_CODE
    many = pipeline_self_backend_host._SELF_BACKEND_HOST_MANY_CODE
    retention = pipeline_self_backend_host._COMPILER_CACHE_RETENTION_HOST_CODE
    plan = pipeline_self_backend_host._SELF_BACKEND_OBJECT_CACHE_PLAN_CODE
    publish = pipeline_self_backend_host._SELF_BACKEND_OBJECT_CACHE_PUBLISH_CODE

    assert "emit_self_asm(text)" in single
    assert "results = pool.map(_emit_one, items, chunksize=1)" in many
    assert "maintain_cache(root, automatic=True" in retention
    assert "pcc.self-backend-object-cache.v2" in many
    assert "object_hash.hexdigest() == expected_checksum" in plan
    assert "os.replace(checksum_tmp_path, checksum_path)" in publish
