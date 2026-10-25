"""Opaque delivery pixels record bounded, encrypted opening signals."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

from galadril_plugins.utils.models import DraftInput, Principal
from galadril_plugins.utils.store import MailError, MailStore
from pydantic import JsonValue

RETENTION_SECONDS = 90 * 86400


class OpenTracking:
    __slots__ = ("store", "origin")

    def __init__(self, store: MailStore, origin: str = "") -> None:
        parsed = urlsplit(origin)
        if origin and (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise MailError("Tracking requires a public HTTPS origin")
        self.store, self.origin = store, origin.rstrip("/")
        with store.database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS email_tracking(
                    token_hash TEXT PRIMARY KEY,tenant TEXT NOT NULL,account TEXT NOT NULL,
                    draft TEXT NOT NULL,secret TEXT NOT NULL,expires INTEGER NOT NULL,
                    UNIQUE(tenant,account,draft),
                    FOREIGN KEY(tenant,account,draft) REFERENCES drafts(tenant,account,id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS tracking_expiration ON email_tracking(expires);
                CREATE TABLE IF NOT EXISTS email_open_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,token_hash TEXT NOT NULL,
                    opened_at INTEGER NOT NULL,address TEXT NOT NULL,
                    FOREIGN KEY(token_hash) REFERENCES email_tracking(token_hash) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS tracking_events ON email_open_events(token_hash,sequence);
            """)

    def prepare(self, actor: Principal, account: str, draft: str) -> str | None:
        """Create pixels only after the approved revision has been claimed for sending."""
        with self.store.database() as db:
            self.store._account(db, actor, account, "send")
            row = db.execute(
                "SELECT content,status FROM drafts WHERE tenant=? AND account=? AND id=?",
                (actor.tenant, account, draft),
            ).fetchone()
            if row is None:
                raise MailError("Draft unavailable")
            content = DraftInput.model_validate_json(
                self.store.decrypt(
                    actor.tenant, account, "draft:" + draft, row["content"]
                )
            )
            if not content.track_opens or row["status"] != "sending":
                return None
            if not self.origin:
                raise MailError("Opening tracking has not been configured")
            prior = db.execute(
                "SELECT secret FROM email_tracking WHERE tenant=? AND account=? AND draft=?",
                (actor.tenant, account, draft),
            ).fetchone()
            if prior:
                token = self.store.decrypt(
                    actor.tenant, account, "tracking:" + draft, prior["secret"]
                ).decode()
            else:
                token = secrets.token_hex(32)
                db.execute(
                    "INSERT INTO email_tracking VALUES(?,?,?,?,?,?)",
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        actor.tenant,
                        account,
                        draft,
                        self.store.encrypt(
                            actor.tenant,
                            account,
                            "tracking:" + draft,
                            token.encode(),
                        ),
                        int(time.time()) + RETENTION_SECONDS,
                    ),
                )
            return self.origin + "/email/open/" + token + ".gif"

    def prune(self) -> None:
        with self.store.database() as db:
            db.execute(
                "DELETE FROM email_tracking WHERE expires<=?",
                (int(time.time()),),
            )

    def record(
        self, token: str, address: str, *, now: int | None = None
    ) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            return
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            return
        stamp = int(time.time()) if now is None else now
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.database() as db:
            db.execute("DELETE FROM email_tracking WHERE expires<=?", (stamp,))
            row = db.execute(
                "SELECT tenant,account,draft FROM email_tracking WHERE token_hash=? AND expires>?",
                (digest, stamp),
            ).fetchone()
            if row is None:
                return
            count = db.execute(
                "SELECT count(*) FROM email_open_events WHERE token_hash=?",
                (digest,),
            ).fetchone()[0]
            if count >= 1000:
                return
            encrypted = self.store.encrypt(
                row["tenant"],
                row["account"],
                "open:" + row["draft"],
                address.encode(),
            )
            db.execute(
                "INSERT INTO email_open_events(token_hash,opened_at,address) VALUES(?,?,?)",
                (digest, stamp, encrypted),
            )

    def history(
        self, actor: Principal, account: str, draft: str, *, before: int = 0
    ) -> dict[str, JsonValue]:
        with self.store.database() as db:
            self.store._account(db, actor, account, "read")
            db.execute(
                "DELETE FROM email_tracking WHERE expires<=?",
                (int(time.time()),),
            )
            rows = db.execute(
                "SELECT e.sequence,e.opened_at,e.address FROM email_open_events e JOIN email_tracking t ON t.token_hash=e.token_hash WHERE t.tenant=? AND t.account=? AND t.draft=? AND (?=0 OR e.sequence<?) ORDER BY e.sequence DESC LIMIT 26",
                (actor.tenant, account, draft, before, before),
            ).fetchall()
            items: list[JsonValue] = [
                {
                    "sequence": row["sequence"],
                    "opened_at": row["opened_at"],
                    "ip": self.store.decrypt(
                        actor.tenant, account, "open:" + draft, row["address"]
                    ).decode(),
                }
                for row in rows[:25]
            ]
            return {
                "items": items,
                "next_before": rows[24]["sequence"] if len(rows) > 25 else 0,
            }
