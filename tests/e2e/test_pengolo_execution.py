"""Exercises generation checkpoints through the real authenticated services."""

import json
import uuid
from collections.abc import Mapping

import pytest
from assertions import eventually, require_mapping
from authentication import initialize_authentication, login_user
from clients import (
    POSTGRES_DSN,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    RelationshipSpec,
    SpiceDBProbe,
    gateway_tls_context,
    seed_users,
)
from environment import pipeline_environment

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_pengolo_checkpoint_smoke() -> None:
    """Checks that small public GraphQL limits do not reject internal checkpoints."""
    gateway = GatewayClient()
    spicedb = SpiceDBProbe()
    try:
        async with pipeline_environment() as environment:
            await eventually(
                environment.authentication_ready,
                timeout_seconds=90,
                description="identity readiness",
            )
            await seed_users()
            initialize_authentication(
                environment.identity_request,
                "https://127.0.0.1:18080",
                POSTGRES_DSN,
                gateway_tls_context(),
            )
            token = await login_user(UPLOADER_ID)
            await spicedb.touch(
                RelationshipSpec(
                    "tenant", TENANT_ID, "member", "user", UPLOADER_ID
                ),
                RelationshipSpec(
                    "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
                ),
            )

            async def model_ready() -> bool:
                value = await gateway.execute(
                    token, "query { pengoloModels { defaultModel } }"
                )
                return (
                    require_mapping(
                        value.get("pengoloModels"), "model catalog"
                    ).get("defaultModel")
                    == "fixture"
                )

            await eventually(
                model_ready,
                timeout_seconds=180,
                description="Scribe model catalog readiness",
            )
            data = await gateway.execute(
                token,
                "mutation($input: PengoloSendInput!) { sendPengolo(input: $input) { generation { id } } }",
                {
                    "input": {
                        "idempotencyKey": str(uuid.uuid4()),
                        "modelAlias": "fixture",
                        "prompt": json.dumps(
                            {
                                "entity_id": "fixture",
                                "causal_entity_id": "fixture",
                                "question": "Verify two",
                                "calculator_only": True,
                                "pengolo": True,
                            }
                        ),
                    }
                },
            )
            sent = require_mapping(data.get("sendPengolo"), "send result")
            identifier = require_mapping(
                sent.get("generation"), "generation"
            ).get("id")
            assert isinstance(identifier, str)

            async def completed() -> Mapping[str, object] | None:
                data = await gateway.execute(
                    token,
                    "query($id:String!) { pengoloEvents(generationId:$id, after:0, temporary:false) { status message { text } } }",
                    {"id": identifier},
                )
                result = require_mapping(
                    data.get("pengoloEvents"), "generation events"
                )
                status = result.get("status")
                if status in {"failed", "cancelled"}:
                    raise AssertionError(
                        f"Native checkpoint generation ended with {status}"
                    )
                return result if status == "completed" else None

            result = await eventually(
                completed,
                timeout_seconds=30,
                description="native checkpoint generation",
                abort_on=(AssertionError,),
            )
            text = require_mapping(
                result.get("message"), "published message"
            ).get("text")
            assert isinstance(text, str)
            answer = require_mapping(json.loads(text), "published calculation")
            assert answer.get("run_python") == 2
    finally:
        await gateway.close()


if __name__ == "__main__":
    raise SystemExit(
        pytest.main([__file__, "--import-mode=importlib", "--capture=no"])
    )
