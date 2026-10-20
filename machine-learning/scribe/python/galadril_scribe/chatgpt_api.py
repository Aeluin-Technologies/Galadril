"""Only Gateway may supply opaque owners to this private account-management API."""

from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from galadril_scribe.chatgpt_accounts import AccountFailure, AccountStatus

if TYPE_CHECKING:
    from galadril_scribe.runtime import Runtime


class AccountOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    operation: Literal[
        "status", "begin", "select", "disconnect", "retry", "callback"
    ]
    owner: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    registration: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    redirect: str | None = Field(default=None, max_length=256)
    state: SecretStr | None = Field(default=None, min_length=32, max_length=256)
    code: SecretStr | None = Field(default=None, max_length=8192)
    client_id: str | None = Field(default=None, max_length=256)
    error: str | None = Field(default=None, max_length=256)


def install_chatgpt(app: FastAPI, runtime: "Runtime") -> None:
    @app.post("/openai/accounts")
    async def accounts(request: Request) -> dict[str, object]:
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > 16384:
                raise HTTPException(413, "Account operation exceeds limit")
            body.extend(part)
        try:
            operation = AccountOperation.model_validate_json(body)
        except ValidationError:
            raise HTTPException(400, "Invalid account operation") from None
        broker = runtime.chatgpt
        if broker is None:
            if operation.operation == "status":
                return AccountStatus(enabled=False).model_dump(mode="json")
            raise HTTPException(503, "ChatGPT plan usage is disabled")
        try:
            if operation.operation == "callback":
                if operation.state is None:
                    raise AccountFailure("Missing authorization state")
                await broker.complete(
                    operation.state.get_secret_value(),
                    code=operation.code.get_secret_value()
                    if operation.code
                    else None,
                    client_id=operation.client_id,
                    error=operation.error,
                )
                return {"completed": True}
            owner = operation.owner
            if owner is None:
                raise AccountFailure("Missing account owner")
            if operation.operation == "begin":
                if operation.redirect is None:
                    raise AccountFailure("Missing callback")
                launch = await broker.begin(
                    owner, operation.redirect, operation.registration
                )
                return {
                    **(await broker.status(owner)).model_dump(mode="json"),
                    "authorization_url": launch.url,
                }
            if operation.operation == "select":
                if operation.registration is None:
                    raise AccountFailure("Missing account registration")
                return (
                    await broker.select(owner, operation.registration)
                ).model_dump(mode="json")
            if operation.operation == "disconnect":
                if operation.registration is None:
                    raise AccountFailure("Missing account registration")
                confirmed = await broker.disconnect(
                    owner, operation.registration
                )
                return {
                    **(await broker.status(owner)).model_dump(mode="json"),
                    "revocation_confirmed": confirmed,
                }
            if operation.operation == "retry":
                return (await broker.resume(owner)).model_dump(mode="json")
            return (await broker.status(owner)).model_dump(mode="json")
        except AccountFailure:
            raise HTTPException(
                400, "ChatGPT account operation could not complete"
            ) from None
