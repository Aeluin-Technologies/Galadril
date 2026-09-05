"""Shared execution properties for integration tests."""

DOCKER_TEST_EXEC_PROPERTIES = {
    "test.init-dockerd": "true",
    "test.workload-isolation-type": "firecracker",
}
