"""Tests the typed tenant lifecycle without a network dependency."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Protocol
from unittest.mock import patch

from galadril_registry_api import RegistryClient, RegistryConfig, registry_pb2


class _Deserializer(Protocol):
    def __call__(self, payload: bytes) -> object: ...


class FakeChannel:
    """Returns serialized fixtures while recording the requested RPC paths."""

    __slots__ = ("calls", "responses")

    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def unary_unary(
        self,
        path: str,
        *,
        request_serializer: Callable[[object], bytes],
        response_deserializer: _Deserializer,
    ) -> Callable[[object], object]:
        async def invoke(
            request: object, *, timeout: float | None = None
        ) -> object:
            request_serializer(request)
            self.calls.append(path)
            payload = self.responses.pop(0)
            return response_deserializer(payload)

        return invoke

    async def close(self) -> None:
        return None


class RegistryClientTenantTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_tenant_discovery_reads_every_page(self) -> None:
        channel = FakeChannel(
            [
                registry_pb2.ListTenantsResponse(
                    tenant_ids=["tenant_a"], next_page_token="opaque-next"
                ).SerializeToString(),
                registry_pb2.ListTenantsResponse(
                    tenant_ids=["tenant_b"]
                ).SerializeToString(),
            ]
        )
        with patch("grpc.aio.insecure_channel", return_value=channel):
            client = RegistryClient(RegistryConfig())
        self.assertEqual(await client.list_tenants(), ("tenant_a", "tenant_b"))
        self.assertEqual(
            channel.calls, ["/galadril.registry.v1.Registry/ListTenants"] * 2
        )

    async def test_native_discovery_rejects_unsafe_identity_and_cursor_cycle(
        self,
    ) -> None:
        for responses in (
            [
                registry_pb2.ListTenantsResponse(
                    tenant_ids=["../foreign"]
                ).SerializeToString()
            ],
            [
                registry_pb2.ListTenantsResponse(
                    next_page_token="cycle"
                ).SerializeToString()
            ]
            * 2,
        ):
            channel = FakeChannel(responses)
            with patch("grpc.aio.insecure_channel", return_value=channel):
                client = RegistryClient(RegistryConfig())
            with self.assertRaises(ValueError):
                await client.list_tenants()

    def test_ontology_authoring_fields_remain_optional_on_the_wire(
        self,
    ) -> None:
        request = registry_pb2.PutOntologyRequest(
            tenant_id="tenant_a", ontology_id="people"
        )
        self.assertFalse(request.HasField("authoring_command_json"))
        request.authoring_command_json = b'{"operation":"create_branch"}'
        decoded = registry_pb2.PutOntologyRequest.FromString(
            request.SerializeToString()
        )
        self.assertEqual(
            decoded.authoring_command_json, request.authoring_command_json
        )
        page = registry_pb2.ListOntologyHistoryResponse(
            items=[
                registry_pb2.OntologyHistoryEntry(
                    revision_id="a" * 20, branch="main"
                )
            ],
            next_revision_id="b" * 20,
        )
        restored = registry_pb2.ListOntologyHistoryResponse.FromString(
            page.SerializeToString()
        )
        self.assertEqual(restored.items[0].branch, "main")
        self.assertTrue(restored.HasField("next_revision_id"))

    async def test_empty_validation_is_rejected_locally(self) -> None:
        channel = FakeChannel([])
        with patch("grpc.aio.insecure_channel", return_value=channel):
            client = RegistryClient(RegistryConfig())

        with self.assertRaises(ValueError):
            await client.validate_tenants(())
        self.assertEqual(channel.calls, [])

    async def test_tenant_lifecycle_is_typed_and_storage_opaque(self) -> None:
        responses = [
            registry_pb2.ValidateTenantsResponse(
                tenants=[
                    registry_pb2.TenantValidation(
                        tenant_id="tenant_a", exists=False
                    )
                ]
            ).SerializeToString(),
            registry_pb2.Tenant(
                tenant_id="tenant_a", head_revision_id="a" * 20
            ).SerializeToString(),
            registry_pb2.Tenant(
                tenant_id="tenant_a", head_revision_id="a" * 20
            ).SerializeToString(),
            registry_pb2.DeleteTenantResponse().SerializeToString(),
        ]
        channel = FakeChannel(responses)
        with patch("grpc.aio.insecure_channel", return_value=channel):
            client = RegistryClient(
                RegistryConfig(endpoint="http://registry:50052")
            )

        validation = await client.validate_tenants(("tenant_a",))
        self.assertEqual(validation[0].tenant_id, "tenant_a")
        self.assertFalse(validation[0].exists)
        self.assertEqual(
            (await client.put_tenant("tenant_a")).tenant_id, "tenant_a"
        )
        self.assertEqual(
            (await client.get_tenant("tenant_a")).tenant_id, "tenant_a"
        )
        await client.delete_tenant(
            "tenant_a", confirmation_tenant_id="tenant_a"
        )
        self.assertEqual(
            channel.calls,
            [
                "/galadril.registry.v1.Registry/ValidateTenants",
                "/galadril.registry.v1.Registry/PutTenant",
                "/galadril.registry.v1.Registry/GetTenant",
                "/galadril.registry.v1.Registry/DeleteTenant",
            ],
        )


if __name__ == "__main__":
    unittest.main()
