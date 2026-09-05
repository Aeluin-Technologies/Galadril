"""TerminusDB wire contract and native revision regressions."""

import asyncio

import galadril_ontology.backends.terminus.client as client_module
import httpx
import pytest
from galadril_ontology.backends.terminus import TerminusClient, TerminusConfig
from galadril_ontology.errors import (
    BranchAlreadyExistsError,
    ConcurrentHeadUpdateError,
    OntologyError,
    OntologyNotFoundError,
)


def config() -> TerminusConfig:
    return TerminusConfig.model_validate(
        {
            "endpoint": "http://localhost:6363",
            "organization": "admin",
            "tenants": {
                "tenant_a": {
                    "database": "tenant_a",
                    "user": "reader_a",
                    "password": "secret",
                }
            },
        }
    )


def test_native_commit_and_compare_and_swap() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"TerminusDB-Data-Version": "branch:oldcommit"},
                json=[{"@id": "pipeline/daily", "name": "daily"}],
            )
        assert request.headers["TerminusDB-Data-Version"] == "branch:oldcommit"
        assert request.url.params["raw_json"] == "true"
        return httpx.Response(
            200,
            headers={"TerminusDB-Data-Version": "branch:newcommit"},
            json=["pipeline/daily"],
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as http:
            client = TerminusClient(config(), http=http)
            version, docs = await client.read("tenant_a")
            assert version == "oldcommit"
            assert (
                await client.write(
                    "tenant_a",
                    docs[0],
                    expected=version,
                    author="alice",
                    message="Edit",
                )
                == "newcommit"
            )
            with pytest.raises(OntologyNotFoundError):
                await client.read("tenant_b")
            with pytest.raises(OntologyNotFoundError):
                await client.read("tenant_a/../tenant_b")
        assert len(requests) == 2
        assert all(
            "/admin/tenant_a/local/branch/main" in str(r.url) for r in requests
        )

    asyncio.run(scenario())


def test_stale_commit_is_not_retried() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, json={"@type": "api:DataVersionMismatch"})

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as http:
            client = TerminusClient(config(), http=http)
            with pytest.raises(ConcurrentHeadUpdateError):
                await client.write(
                    "tenant_a",
                    {"@id": "pipeline/daily"},
                    expected="old",
                    author="alice",
                    message="Edit",
                )

    asyncio.run(scenario())
    assert calls == 1


def test_duplicate_tenant_databases_are_rejected() -> None:
    data = config().model_dump()
    data["tenants"]["tenant_b"] = data["tenants"]["tenant_a"]
    with pytest.raises(ValueError):
        TerminusConfig.model_validate(data)


@pytest.mark.parametrize(
    "update",
    (
        {"endpoint": "ftp://localhost"},
        {"tenants": {"bad/tenant": config().tenants["tenant_a"]}},
        {
            "tenants": {
                "tenant_a": {
                    "database": "tenant_a",
                    "user": "admin",
                    "password": "secret",
                }
            }
        },
    ),
)
def test_invalid_capability_configuration_is_rejected(
    update: dict[str, object],
) -> None:
    data = config().model_dump()
    data.update(update)
    with pytest.raises(ValueError):
        TerminusConfig.model_validate(data)


def test_transport_maps_all_server_failures_to_domain_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases: tuple[tuple[int, object, type[Exception]], ...] = (
        (400, {"@type": "api:BranchExistsError"}, BranchAlreadyExistsError),
        (404, {}, OntologyNotFoundError),
        (500, {}, OntologyError),
    )

    async def scenario() -> None:
        for status, payload, error_type in cases:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, status=status, payload=payload: (
                        httpx.Response(status, json=payload)
                    )
                )
            ) as http:
                with pytest.raises(error_type):
                    await TerminusClient(config(), http=http).read("tenant_a")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"TerminusDB-Data-Version": "branch:bad/ref"},
                    json=[],
                )
            )
        ) as http:
            with pytest.raises(OntologyError, match="Invalid"):
                await TerminusClient(config(), http=http).read("tenant_a")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=[])
            )
        ) as http:
            client = TerminusClient(config(), http=http)
            with pytest.raises(OntologyError, match="omitted"):
                await client.read("tenant_a")
            with pytest.raises(OntologyError, match="omitted"):
                await client.write(
                    "tenant_a",
                    {"@id": "ontology/state"},
                    expected="old",
                    author="alice",
                    message="Edit",
                )

        def disconnect(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(disconnect)
        ) as http:
            with pytest.raises(OntologyError, match="unavailable"):
                await TerminusClient(config(), http=http).read("tenant_a")

        monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"[]")
            )
        ) as http:
            client = TerminusClient(config(), http=http)
            with pytest.raises(OntologyError, match="safety limit"):
                await client.read("tenant_a")

        with pytest.raises(ValueError, match="Invalid native"):
            client.path("tenant_a", "bad/ref")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error_type",
    ["api:UnresolvableAbsoluteDescriptor", "api:BranchDoesNotExistError"],
)
def test_missing_native_branch_is_a_domain_absence(error_type: str) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    400, json={"api:error": {"@type": error_type}}
                )
            )
        ) as http:
            with pytest.raises(OntologyNotFoundError):
                await TerminusClient(config(), http=http).read(
                    "tenant_a", ref="missing"
                )

    asyncio.run(scenario())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
