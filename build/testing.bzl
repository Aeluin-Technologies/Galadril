"""Shared execution properties for integration tests."""

DOCKER_TEST_EXEC_PROPERTIES = {
    # The PostgreSQL image alone needs over 10 GB after extraction.
    "test.EstimatedFreeDiskBytes": "30GB",
    "test.init-dockerd": "true",
    "test.workload-isolation-type": "firecracker",
}

DOCKER_E2E_TEST_EXEC_PROPERTIES = dict(DOCKER_TEST_EXEC_PROPERTIES, **{
    "test.EstimatedComputeUnits": "6",
})
