"""Transactional mail state with purpose-bound encryption and independent ACLs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from galadril_plugins.utils.models import (
    RIGHTS,
    ConnectionInput,
    Credentials,
    DraftInput,
    MailMessage,
    Principal,
    ResponseProfile,
    Right,
)
from pydantic import JsonValue


class MailError(RuntimeError):
    """Public errors contain neither provider responses nor connection secrets."""


class MailStore:
    __slots__ = ("path", "_keys", "_active")

    def __init__(
        self, path: Path, keys: Mapping[str, bytes], active: str
    ) -> None:
        if active not in keys or any(
            len(key) != 32 or ":" in name for name, key in keys.items()
        ):
            raise MailError("Invalid encryption keyring")
        self.path, self._active = path, active
        self._keys = {name: AESGCM(key) for name, key in keys.items()}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.database() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts(
                    tenant TEXT NOT NULL,id TEXT NOT NULL,owner TEXT NOT NULL,
                    revision INTEGER NOT NULL,config TEXT NOT NULL,secrets TEXT NOT NULL,
                    profile TEXT,updated INTEGER NOT NULL,last_sync INTEGER NOT NULL DEFAULT 0,
                    sync_status TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(tenant,id));
                CREATE TABLE IF NOT EXISTS grants(
                    tenant TEXT NOT NULL,account TEXT NOT NULL,user TEXT NOT NULL,rights TEXT NOT NULL,
                    PRIMARY KEY(tenant,account,user),FOREIGN KEY(tenant,account) REFERENCES accounts(tenant,id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS cursors(
                    tenant TEXT NOT NULL,account TEXT NOT NULL,folder TEXT NOT NULL,validity INTEGER NOT NULL,uid INTEGER NOT NULL,
                    PRIMARY KEY(tenant,account,folder),FOREIGN KEY(tenant,account) REFERENCES accounts(tenant,id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS messages(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,account TEXT NOT NULL,id TEXT NOT NULL,
                    folder TEXT NOT NULL,validity INTEGER NOT NULL,uid INTEGER NOT NULL,content TEXT NOT NULL,
                    ingested INTEGER NOT NULL DEFAULT 0,processed INTEGER NOT NULL DEFAULT 0,label TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL,UNIQUE(tenant,account,folder,validity,uid),UNIQUE(tenant,account,id),
                    FOREIGN KEY(tenant,account) REFERENCES accounts(tenant,id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS drafts(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,account TEXT NOT NULL,id TEXT NOT NULL,
                    revision INTEGER NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,approved INTEGER,
                    delivery_id TEXT NOT NULL,source_id TEXT,automatic INTEGER NOT NULL DEFAULT 0,created INTEGER NOT NULL,
                    UNIQUE(tenant,account,id),FOREIGN KEY(tenant,account) REFERENCES accounts(tenant,id) ON DELETE CASCADE);
                CREATE UNIQUE INDEX IF NOT EXISTS one_response_per_source ON drafts(tenant,account,source_id) WHERE source_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS auto_actions(
                    tenant TEXT NOT NULL,account TEXT NOT NULL,draft TEXT NOT NULL,thread TEXT NOT NULL,sender TEXT NOT NULL,created INTEGER NOT NULL,
                    PRIMARY KEY(tenant,account,draft),FOREIGN KEY(tenant,account,draft) REFERENCES drafts(tenant,account,id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS auto_threads ON auto_actions(tenant,account,thread);
                CREATE INDEX IF NOT EXISTS auto_senders ON auto_actions(tenant,account,sender,created);
                CREATE TABLE IF NOT EXISTS audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,account TEXT NOT NULL,
                    actor TEXT NOT NULL,action TEXT NOT NULL,created INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS mail_accounts ON messages(tenant,account,sequence);
                CREATE INDEX IF NOT EXISTS pending_ingestion ON messages(tenant,account,ingested,sequence);
            """)

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def encrypt(
        self, tenant: str, identifier: str, purpose: str, value: bytes
    ) -> str:
        nonce = os.urandom(12)
        encrypted = self._keys[self._active].encrypt(
            nonce, value, json.dumps((tenant, identifier, purpose)).encode()
        )
        return (
            self._active
            + ":"
            + base64.b64encode(nonce + encrypted).decode("ascii")
        )

    def decrypt(
        self, tenant: str, identifier: str, purpose: str, value: str
    ) -> bytes:
        try:
            key, encoded = value.split(":", 1)
            data = base64.b64decode(encoded, validate=True)
            return self._keys[key].decrypt(
                data[:12],
                data[12:],
                json.dumps((tenant, identifier, purpose)).encode(),
            )
        except Exception:
            raise MailError("Encrypted connection data unavailable") from None

    def _credentials(
        self, actor: Principal, identifier: str, value: Credentials
    ) -> str:
        content = value.model_dump(mode="json")
        content["password"] = value.password.get_secret_value()
        content["refresh_token"] = (
            value.refresh_token.get_secret_value()
            if value.refresh_token
            else None
        )
        return self.encrypt(
            actor.tenant,
            identifier,
            "credentials",
            json.dumps(content).encode(),
        )

    @staticmethod
    def _rights(
        db: sqlite3.Connection, actor: Principal, account: sqlite3.Row
    ) -> tuple[Right, ...]:
        if account["owner"] == actor.user:
            return RIGHTS
        row = db.execute(
            "SELECT rights FROM grants WHERE tenant=? AND account=? AND user=?",
            (actor.tenant, account["id"], actor.user),
        ).fetchone()
        if row is None:
            return ()
        granted = json.loads(row["rights"])
        rights = tuple(right for right in RIGHTS if right in granted)
        return ("view", *rights) if rights and "view" not in rights else rights

    def _account(
        self,
        db: sqlite3.Connection,
        actor: Principal,
        identifier: str,
        right: Right,
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM accounts WHERE tenant=? AND id=?",
            (actor.tenant, identifier),
        ).fetchone()
        if row is None or right not in self._rights(db, actor, row):
            raise MailError("FORBIDDEN")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _audit(
        db: sqlite3.Connection, actor: Principal, identifier: str, action: str
    ) -> None:
        db.execute(
            "INSERT INTO audit(tenant,account,actor,action,created) VALUES(?,?,?,?,?)",
            (actor.tenant, identifier, actor.user, action, int(time.time())),
        )

    def _public(
        self, db: sqlite3.Connection, actor: Principal, row: sqlite3.Row
    ) -> dict[str, JsonValue]:
        return {
            "id": row["id"],
            "revision": row["revision"],
            "config": json.loads(row["config"]),
            "rights": list(self._rights(db, actor, row)),
            "last_sync": row["last_sync"],
            "sync_status": row["sync_status"],
            "has_credentials": True,
            "can_manage_access": row["owner"] == actor.user,
        }

    def create(
        self, actor: Principal, value: ConnectionInput, credentials: Credentials
    ) -> dict[str, JsonValue]:
        identifier = uuid.uuid4().hex
        with self.database() as db:
            count = db.execute(
                "SELECT count(*) FROM accounts WHERE tenant=?", (actor.tenant,)
            ).fetchone()[0]
            if count >= 100:
                raise MailError("Connection limit reached")
            db.execute(
                "INSERT INTO accounts(tenant,id,owner,revision,config,secrets,updated) VALUES(?,?,?,1,?,?,?)",
                (
                    actor.tenant,
                    identifier,
                    actor.user,
                    value.model_dump_json(),
                    self._credentials(actor, identifier, credentials),
                    int(time.time()),
                ),
            )
            self._audit(db, actor, identifier, "created")
            return self._public(
                db, actor, self._account(db, actor, identifier, "view")
            )

    def accounts(self, actor: Principal) -> list[dict[str, JsonValue]]:
        with self.database() as db:
            rows = db.execute(
                "SELECT * FROM accounts WHERE tenant=? ORDER BY id LIMIT 100",
                (actor.tenant,),
            ).fetchall()
            return [
                self._public(db, actor, row)
                for row in rows
                if "view" in self._rights(db, actor, row)
            ]

    def connection(
        self, actor: Principal, identifier: str, right: Right
    ) -> tuple[ConnectionInput, Credentials]:
        with self.database() as db:
            row = self._account(db, actor, identifier, right)
            return ConnectionInput.model_validate_json(
                row["config"]
            ), Credentials.model_validate_json(
                self.decrypt(
                    actor.tenant, identifier, "credentials", row["secrets"]
                )
            )

    def reveal(self, actor: Principal, identifier: str) -> Credentials:
        with self.database() as db:
            row = self._account(db, actor, identifier, "reveal")
            self._audit(db, actor, identifier, "credentials_revealed")
            return Credentials.model_validate_json(
                self.decrypt(
                    actor.tenant, identifier, "credentials", row["secrets"]
                )
            )

    def update(
        self,
        actor: Principal,
        identifier: str,
        revision: int,
        value: ConnectionInput,
        credentials: Credentials | None,
    ) -> dict[str, JsonValue]:
        with self.database() as db:
            row = self._account(db, actor, identifier, "edit")
            if row["revision"] != revision:
                raise MailError("CONFLICT")
            old = ConnectionInput.model_validate_json(row["config"])
            # A write-only editor must not redirect existing credentials to an attacker.
            if credentials is None and (
                old.imap_host,
                old.smtp_host,
                old.address,
            ) != (value.imap_host, value.smtp_host, value.address):
                raise MailError(
                    "Supply replacement credentials when changing servers or identity"
                )
            secrets = (
                self._credentials(actor, identifier, credentials)
                if credentials
                else row["secrets"]
            )
            db.execute(
                "UPDATE accounts SET config=?,secrets=?,revision=revision+1,updated=?,sync_status='pending' WHERE tenant=? AND id=?",
                (
                    value.model_dump_json(),
                    secrets,
                    int(time.time()),
                    actor.tenant,
                    identifier,
                ),
            )
            db.execute(
                "UPDATE drafts SET approved=NULL,status='draft' WHERE tenant=? AND account=? AND status='approved'",
                (actor.tenant, identifier),
            )
            self._audit(db, actor, identifier, "updated")
            return self._public(
                db, actor, self._account(db, actor, identifier, "edit")
            )

    def delete(self, actor: Principal, identifier: str, revision: int) -> None:
        with self.database() as db:
            row = self._account(db, actor, identifier, "delete")
            if row["revision"] != revision:
                raise MailError("CONFLICT")
            self._audit(db, actor, identifier, "deleted")
            db.execute(
                "DELETE FROM accounts WHERE tenant=? AND id=?",
                (actor.tenant, identifier),
            )

    def grant(
        self,
        actor: Principal,
        identifier: str,
        user: str,
        rights: tuple[Right, ...],
    ) -> None:
        Principal(tenant=actor.tenant, user=user)
        if len(set(rights)) != len(rights) or any(
            right not in RIGHTS for right in rights
        ):
            raise MailError("Invalid permissions")
        with self.database() as db:
            row = self._account(db, actor, identifier, "share")
            # Only the owner can delegate authority; sharing does not imply secret access.
            if row["owner"] != actor.user:
                raise MailError("FORBIDDEN")
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?) ON CONFLICT(tenant,account,user) DO UPDATE SET rights=excluded.rights",
                (actor.tenant, identifier, user, json.dumps(rights)),
            )
            self._audit(db, actor, identifier, "permissions_changed")

    def grants(
        self, actor: Principal, identifier: str
    ) -> list[dict[str, JsonValue]]:
        with self.database() as db:
            self._account(db, actor, identifier, "share")
            return [
                {"user": row["user"], "rights": json.loads(row["rights"])}
                for row in db.execute(
                    "SELECT user,rights FROM grants WHERE tenant=? AND account=?",
                    (actor.tenant, identifier),
                )
            ]

    def cursor(
        self, actor: Principal, identifier: str, folder: str
    ) -> tuple[int | None, int]:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            row = db.execute(
                "SELECT validity,uid FROM cursors WHERE tenant=? AND account=? AND folder=?",
                (actor.tenant, identifier, folder),
            ).fetchone()
            return (row["validity"], row["uid"]) if row else (None, 0)

    def record_message(
        self,
        actor: Principal,
        identifier: str,
        folder: str,
        validity: int,
        uid: int,
        message: MailMessage,
    ) -> str:
        key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{actor.tenant}/{identifier}/{folder}/{validity}/{uid}",
        ).hex
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            content = self.encrypt(
                actor.tenant,
                identifier,
                "message:" + key,
                message.model_dump_json().encode(),
            )
            db.execute(
                "INSERT INTO messages(tenant,account,id,folder,validity,uid,content,created) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant,account,folder,validity,uid) DO NOTHING",
                (
                    actor.tenant,
                    identifier,
                    key,
                    folder,
                    validity,
                    uid,
                    content,
                    int(time.time()),
                ),
            )
            db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?,?) ON CONFLICT(tenant,account,folder) DO UPDATE SET validity=excluded.validity,uid=CASE WHEN cursors.validity=excluded.validity THEN max(cursors.uid,excluded.uid) ELSE excluded.uid END",
                (actor.tenant, identifier, folder, validity, uid),
            )
        return key

    def advance(
        self,
        actor: Principal,
        identifier: str,
        folder: str,
        validity: int,
        uid: int,
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?,?) ON CONFLICT(tenant,account,folder) DO UPDATE SET validity=excluded.validity,uid=CASE WHEN cursors.validity=excluded.validity THEN max(cursors.uid,excluded.uid) ELSE excluded.uid END",
                (actor.tenant, identifier, folder, validity, uid),
            )

    def _mail(
        self, actor: Principal, identifier: str, row: sqlite3.Row
    ) -> MailMessage:
        return MailMessage.model_validate_json(
            self.decrypt(
                actor.tenant, identifier, "message:" + row["id"], row["content"]
            )
        )

    def _messages(
        self,
        actor: Principal,
        identifier: str,
        *,
        before: int = 0,
        query: str = "",
        pending: bool = False,
    ) -> tuple[list[dict[str, JsonValue]], int]:
        with self.database() as db:
            self._account(db, actor, identifier, "use" if pending else "read")
            rows = db.execute(
                "SELECT * FROM messages WHERE tenant=? AND account=? AND (?=0 OR sequence<?) AND (?=0 OR ingested=0) ORDER BY sequence DESC LIMIT 25",
                (actor.tenant, identifier, before, before, int(pending)),
            )
            output: list[dict[str, JsonValue]] = []
            count, last = 0, 0
            for row in rows:
                count += 1
                last = row["sequence"]
                message = self._mail(actor, identifier, row)
                if (
                    query
                    and query.casefold()
                    not in (
                        message.subject
                        + " "
                        + message.sender
                        + " "
                        + message.body
                    ).casefold()
                ):
                    continue
                output.append(
                    {
                        "id": row["id"],
                        "sequence": row["sequence"],
                        "folder": row["folder"],
                        "label": row["label"],
                        "ingested": bool(row["ingested"]),
                        "message": message.model_copy(
                            update={
                                "body": message.body[:4096],
                                "attachments": tuple(
                                    item.model_copy(update={"content": ""})
                                    for item in message.attachments
                                ),
                            }
                        ).model_dump(mode="json"),
                    }
                )
            return output, last if count == 25 else 0

    def messages(
        self,
        actor: Principal,
        identifier: str,
        *,
        before: int = 0,
        query: str = "",
        pending: bool = False,
    ) -> list[dict[str, JsonValue]]:
        return self._messages(
            actor, identifier, before=before, query=query, pending=pending
        )[0]

    def message_page(
        self,
        actor: Principal,
        identifier: str,
        *,
        before: int = 0,
        query: str = "",
    ) -> dict[str, JsonValue]:
        items, cursor = self._messages(
            actor, identifier, before=before, query=query
        )
        return {"items": [item for item in items], "next_before": cursor}

    def message(
        self,
        actor: Principal,
        identifier: str,
        message_id: str,
        right: Right = "read",
    ) -> MailMessage:
        with self.database() as db:
            self._account(db, actor, identifier, right)
            row = db.execute(
                "SELECT * FROM messages WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, message_id),
            ).fetchone()
            if row is None:
                raise MailError("Message unavailable")
            return self._mail(actor, identifier, row)

    def mark_ingested(
        self, actor: Principal, identifier: str, message_id: str
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            db.execute(
                "UPDATE messages SET ingested=1 WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, message_id),
            )

    def label(
        self,
        actor: Principal,
        identifier: str,
        message_id: str,
        label: str,
        right: Right = "edit",
    ) -> None:
        if len(label) > 128:
            raise MailError("Invalid label")
        with self.database() as db:
            self._account(db, actor, identifier, right)
            db.execute(
                "UPDATE messages SET label=? WHERE tenant=? AND account=? AND id=?",
                (label, actor.tenant, identifier, message_id),
            )
            self._audit(db, actor, identifier, "message_classified")

    def save_draft(
        self,
        actor: Principal,
        identifier: str,
        draft_id: str | None,
        revision: int | None,
        value: DraftInput,
        *,
        source_id: str | None = None,
        automatic: bool = False,
    ) -> dict[str, JsonValue]:
        draft_id = draft_id or uuid.uuid4().hex
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            row = db.execute(
                "SELECT * FROM drafts WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, draft_id),
            ).fetchone()
            content = self.encrypt(
                actor.tenant,
                identifier,
                "draft:" + draft_id,
                value.model_dump_json().encode(),
            )
            if row:
                if row["revision"] != revision or row["status"] in {
                    "sending",
                    "sent",
                    "uncertain",
                }:
                    raise MailError("CONFLICT")
                db.execute(
                    "UPDATE drafts SET revision=revision+1,content=?,approved=NULL,status='draft' WHERE tenant=? AND account=? AND id=?",
                    (content, actor.tenant, identifier, draft_id),
                )
            else:
                if revision is not None:
                    raise MailError("CONFLICT")
                db.execute(
                    "INSERT INTO drafts(tenant,account,id,revision,content,status,delivery_id,source_id,automatic,created) VALUES(?,?,?,1,?,'draft',?,?,?,?)",
                    (
                        actor.tenant,
                        identifier,
                        draft_id,
                        content,
                        f"<{uuid.uuid4().hex}@galadril.local>",
                        source_id,
                        int(automatic),
                        int(time.time()),
                    ),
                )
            self._audit(db, actor, identifier, "draft_saved")
            row = db.execute(
                "SELECT * FROM drafts WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, draft_id),
            ).fetchone()
            return self._draft(actor, identifier, row)

    def _draft(
        self,
        actor: Principal,
        identifier: str,
        row: sqlite3.Row,
        *,
        preview: bool = False,
    ) -> dict[str, JsonValue]:
        content = DraftInput.model_validate_json(
            self.decrypt(
                actor.tenant, identifier, "draft:" + row["id"], row["content"]
            )
        )
        if preview:
            content = content.model_copy(
                update={
                    "body": content.body[:4096],
                    "attachments": tuple(
                        item.model_copy(update={"content": ""})
                        for item in content.attachments
                    ),
                }
            )
        return {
            "id": row["id"],
            "revision": row["revision"],
            "status": row["status"],
            "delivery_id": row["delivery_id"],
            "automatic": bool(row["automatic"]),
            "source_id": row["source_id"],
            "content": content.model_dump(mode="json"),
        }

    def drafts(
        self, actor: Principal, identifier: str
    ) -> list[dict[str, JsonValue]]:
        with self.database() as db:
            self._account(db, actor, identifier, "read")
            return [
                self._draft(actor, identifier, row, preview=True)
                for row in db.execute(
                    "SELECT * FROM drafts WHERE tenant=? AND account=? ORDER BY sequence DESC LIMIT 25",
                    (actor.tenant, identifier),
                )
            ]

    def draft(
        self,
        actor: Principal,
        identifier: str,
        draft_id: str,
        right: Right = "read",
    ) -> dict[str, JsonValue]:
        with self.database() as db:
            self._account(db, actor, identifier, right)
            row = db.execute(
                "SELECT * FROM drafts WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, draft_id),
            ).fetchone()
            if row is None:
                raise MailError("Draft unavailable")
            return self._draft(actor, identifier, row)

    def approve(
        self, actor: Principal, identifier: str, draft_id: str, revision: int
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "approve")
            result = db.execute(
                "UPDATE drafts SET approved=revision,status='approved' WHERE tenant=? AND account=? AND id=? AND revision=? AND status IN ('draft','approved')",
                (actor.tenant, identifier, draft_id, revision),
            )
            if result.rowcount != 1:
                raise MailError("CONFLICT")
            self._audit(db, actor, identifier, "draft_approved")

    def claim_send(
        self, actor: Principal, identifier: str, draft_id: str, revision: int
    ) -> DraftInput:
        with self.database() as db:
            account = self._account(db, actor, identifier, "send")
            if not ConnectionInput.model_validate_json(
                account["config"]
            ).enabled:
                raise MailError("Connection is disabled")
            row = db.execute(
                "SELECT * FROM drafts WHERE tenant=? AND account=? AND id=? AND revision=? AND approved=revision AND status='approved'",
                (actor.tenant, identifier, draft_id, revision),
            ).fetchone()
            if row is None:
                raise MailError(
                    "Draft requires approval or delivery reconciliation"
                )
            # The claim commits before SMTP; a process crash leaves a non-retryable state.
            db.execute(
                "UPDATE drafts SET status='sending' WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, draft_id),
            )
            self._audit(db, actor, identifier, "send_claimed")
            return DraftInput.model_validate_json(
                self.decrypt(
                    actor.tenant,
                    identifier,
                    "draft:" + draft_id,
                    row["content"],
                )
            )

    def finish_send(
        self, actor: Principal, identifier: str, draft_id: str, success: bool
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "send")
            db.execute(
                "UPDATE drafts SET status=? WHERE tenant=? AND account=? AND id=? AND status='sending'",
                (
                    "sent" if success else "uncertain",
                    actor.tenant,
                    identifier,
                    draft_id,
                ),
            )
            self._audit(
                db, actor, identifier, "sent" if success else "send_uncertain"
            )

    def profile(
        self, actor: Principal, identifier: str
    ) -> ResponseProfile | None:
        with self.database() as db:
            row = self._account(db, actor, identifier, "use")
            return (
                ResponseProfile.model_validate_json(
                    self.decrypt(
                        actor.tenant, identifier, "profile", row["profile"]
                    )
                )
                if row["profile"]
                else None
            )

    def save_profile(
        self, actor: Principal, identifier: str, value: ResponseProfile
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "edit")
            if value.mode == "automatic":
                self._account(db, actor, identifier, "automate")
                if not value.allowed_senders:
                    raise MailError(
                        "Automatic replies require explicitly allowed senders"
                    )
            db.execute(
                "UPDATE accounts SET profile=? WHERE tenant=? AND id=?",
                (
                    self.encrypt(
                        actor.tenant,
                        identifier,
                        "profile",
                        value.model_dump_json().encode(),
                    ),
                    actor.tenant,
                    identifier,
                ),
            )
            self._audit(db, actor, identifier, "profile_updated")

    def workers(self) -> list[tuple[Principal, str, ConnectionInput]]:
        with self.database() as db:
            return [
                (
                    Principal(tenant=row["tenant"], user=row["owner"]),
                    row["id"],
                    ConnectionInput.model_validate_json(row["config"]),
                )
                for row in db.execute(
                    "SELECT tenant,id,owner,config FROM accounts ORDER BY last_sync LIMIT 1000"
                )
            ]

    def sync_status(
        self, actor: Principal, identifier: str, status: str
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            db.execute(
                "UPDATE accounts SET last_sync=?,sync_status=? WHERE tenant=? AND id=?",
                (int(time.time()), status, actor.tenant, identifier),
            )

    def rotate(
        self, actor: Principal, identifier: str, credentials: Credentials
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            db.execute(
                "UPDATE accounts SET secrets=? WHERE tenant=? AND id=?",
                (
                    self._credentials(actor, identifier, credentials),
                    actor.tenant,
                    identifier,
                ),
            )

    def pending(
        self, actor: Principal, identifier: str
    ) -> list[tuple[str, MailMessage]]:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            return [
                (row["id"], self._mail(actor, identifier, row))
                for row in db.execute(
                    "SELECT * FROM messages WHERE tenant=? AND account=? AND processed=0 ORDER BY sequence LIMIT 25",
                    (actor.tenant, identifier),
                )
            ]

    def processed(
        self, actor: Principal, identifier: str, message_id: str
    ) -> None:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            db.execute(
                "UPDATE messages SET processed=1 WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, message_id),
            )

    def approve_automatic(
        self,
        actor: Principal,
        identifier: str,
        draft_id: str,
        source: MailMessage,
    ) -> None:
        with self.database() as db:
            row = self._account(db, actor, identifier, "automate")
            if row["owner"] != actor.user or not row["profile"]:
                raise MailError("FORBIDDEN")
            profile = ResponseProfile.model_validate_json(
                self.decrypt(
                    actor.tenant, identifier, "profile", row["profile"]
                )
            )
            draft = db.execute(
                "SELECT * FROM drafts WHERE tenant=? AND account=? AND id=? AND status='draft' AND automatic=1",
                (actor.tenant, identifier, draft_id),
            ).fetchone()
            if draft is None:
                raise MailError("Automatic reply unavailable")
            content = DraftInput.model_validate_json(
                self.decrypt(
                    actor.tenant,
                    identifier,
                    "draft:" + draft_id,
                    draft["content"],
                )
            )
            config = ConnectionInput.model_validate_json(row["config"])
            if (
                profile.mode != "automatic"
                or not source.reply_allowed
                or source.sender not in profile.allowed_senders
                or source.sender.casefold() == config.address.casefold()
                or config.address.casefold()
                not in {item.casefold() for item in source.recipients}
                or content.to != (source.sender,)
                or content.cc
                or content.attachments
                or content.reply_to_message_id != source.message_id
            ):
                raise MailError("Automatic reply requires human approval")
            used = db.execute(
                "SELECT count(*) FROM drafts WHERE tenant=? AND account=? AND automatic=1 AND status IN ('approved','sending','sent','uncertain') AND created>=?",
                (actor.tenant, identifier, int(time.time()) - 86400),
            ).fetchone()[0]
            if used >= profile.daily_limit:
                raise MailError("Automatic reply daily limit reached")
            thread = hashlib.sha256(
                (
                    identifier + "\0" + (source.thread_id or source.message_id)
                ).encode()
            ).hexdigest()
            sender = hashlib.sha256(
                (identifier + "\0" + source.sender.casefold()).encode()
            ).hexdigest()
            if profile.sender_daily_limit is not None:
                count = db.execute(
                    "SELECT count(*) FROM auto_actions a JOIN drafts d ON d.tenant=a.tenant AND d.account=a.account AND d.id=a.draft WHERE a.tenant=? AND a.account=? AND a.sender=? AND a.created>=? AND d.status IN ('approved','sending','sent','uncertain')",
                    (
                        actor.tenant,
                        identifier,
                        sender,
                        int(time.time()) - 86400,
                    ),
                ).fetchone()[0]
                if count >= profile.sender_daily_limit:
                    raise MailError("Configured sender reply limit reached")
            if (
                profile.one_reply_per_thread
                and db.execute(
                    "SELECT 1 FROM auto_actions a JOIN drafts d ON d.tenant=a.tenant AND d.account=a.account AND d.id=a.draft WHERE a.tenant=? AND a.account=? AND a.thread=? AND d.status IN ('approved','sending','sent','uncertain') LIMIT 1",
                    (actor.tenant, identifier, thread),
                ).fetchone()
            ):
                raise MailError(
                    "This conversation already has an automatic acknowledgment"
                )
            db.execute(
                "INSERT INTO auto_actions VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,account,draft) DO UPDATE SET thread=excluded.thread,sender=excluded.sender,created=excluded.created",
                (
                    actor.tenant,
                    identifier,
                    draft_id,
                    thread,
                    sender,
                    int(time.time()),
                ),
            )
            db.execute(
                "UPDATE drafts SET status='approved',approved=revision WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, identifier, draft_id),
            )
            self._audit(db, actor, identifier, "automatic_reply_authorized")

    def has_response(
        self, actor: Principal, identifier: str, source_id: str
    ) -> bool:
        with self.database() as db:
            self._account(db, actor, identifier, "use")
            return (
                db.execute(
                    "SELECT 1 FROM drafts WHERE tenant=? AND account=? AND source_id=?",
                    (actor.tenant, identifier, source_id),
                ).fetchone()
                is not None
            )

    def audit(
        self, actor: Principal, identifier: str
    ) -> list[dict[str, JsonValue]]:
        with self.database() as db:
            self._account(db, actor, identifier, "share")
            return [
                dict(row)
                for row in db.execute(
                    "SELECT actor,action,created FROM audit WHERE tenant=? AND account=? ORDER BY sequence DESC LIMIT 100",
                    (actor.tenant, identifier),
                )
            ]
