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
        async def invoke(request: object) -> object:
            request_serializer(request)
            self.calls.append(path)
            payload = self.responses.pop(0)
            return response_deserializer(payload)

        return invoke

    async def close(self) -> None:
        return None


class RegistryClientTenantTest(unittest.IsolatedAsyncioTestCase):
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
