"""Bound MIME expansion and preserve protocol identity separately from content."""

from __future__ import annotations

import base64
import hashlib
import re
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import Policy
from email.utils import formatdate, getaddresses, parseaddr
from html import escape
from html.parser import HTMLParser

from galadril_plugins.utils.models import (
    Attachment,
    DraftInput,
    MailMessage,
)
from galadril_plugins.utils.signature import render_signature
from galadril_plugins.utils.store import MailError

MAX_MESSAGE_BYTES = 12 * 1024 * 1024


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "div", "br", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def parse_message(raw: bytes) -> MailMessage:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MailError("Message exceeds size limit")
    created = 0

    def bounded_message(
        policy: Policy[EmailMessage[str, str]],
    ) -> EmailMessage[str, str]:
        nonlocal created
        created += 1
        if created > 128:
            raise MailError("MIME complexity exceeds limit")
        return EmailMessage(policy=policy)

    # Reject MIME fan-out during construction, before a large tree is allocated.
    message = BytesParser(
        policy=policy.default.clone(message_factory=bounded_message)
    ).parsebytes(raw)
    text: list[str] = []
    html: list[str] = []
    attachments: list[Attachment] = []
    total = 0
    count = 0
    for part in message.walk():
        count += 1
        if count > 128:
            raise MailError("MIME complexity exceeds limit")
        if part.is_multipart():
            continue
        payload: object = part.get_payload(decode=True)
        if payload is not None and not isinstance(payload, bytes):
            raise MailError("Invalid MIME payload")
        content = payload or b""
        total += len(content)
        if total > MAX_MESSAGE_BYTES:
            raise MailError("Decoded message exceeds size limit")
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment":
            if len(attachments) >= 32:
                raise MailError("Attachment count exceeds limit")
            attachments.append(
                Attachment(
                    name=(filename or "attachment")[:255],
                    content_type=part.get_content_type(),
                    content=base64.b64encode(content).decode("ascii"),
                )
            )
        elif part.get_content_type() == "text/plain":
            try:
                text.append(
                    content.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                )
            except LookupError:
                text.append(content.decode("utf-8", errors="replace"))
        elif part.get_content_type() == "text/html":
            html.append(content.decode("utf-8", errors="replace"))
    if text:
        body = "\n".join(text)
    else:
        visible = _VisibleText()
        visible.feed("\n".join(html))
        visible.close()
        body = "".join(visible.parts)
    if len(body.encode()) > 1024 * 1024:
        raise MailError("Message text exceeds size limit")
    automatic = str(message.get("Auto-Submitted", "no")).strip().lower() != "no"
    bulk = str(message.get("Precedence", "")).strip().lower() in {
        "bulk",
        "list",
        "junk",
    }
    sender = parseaddr(str(message.get("From", "")))[1]
    identifier = (
        str(message.get("Message-ID", ""))[:998]
        or f"<{hashlib.sha256(raw).hexdigest()}@missing.local>"
    )
    references = str(message.get("References", ""))[:8192]
    roots = re.findall(
        r"<[^<>\s]+>", references or str(message.get("In-Reply-To", ""))[:998]
    )
    reference = str(message.get("X-Galadril-Reference", ""))
    return MailMessage(
        reference=reference
        if re.fullmatch(r"GDR-[A-F0-9]{32}", reference)
        else "",
        message_id=identifier,
        thread_id=roots[0] if roots else identifier,
        sender=sender[:320],
        recipients=tuple(
            value
            for _, value in getaddresses(
                message.get_all("To", []) + message.get_all("Cc", [])
            )
        )[:100],
        subject=str(message.get("Subject", ""))[:998],
        date=str(message.get("Date", ""))[:128],
        body=body,
        references=references,
        reply_allowed=bool(sender)
        and not automatic
        and not bulk
        and not message.get("List-Id")
        and not message.get("X-Auto-Response-Suppress")
        and str(message.get("Return-Path", "")).strip() != "<>",
        attachments=tuple(attachments),
    )


def compose_reply(
    sender: str,
    value: DraftInput,
    delivery_id: str,
    *,
    automatic: bool = False,
    signature_html: str = "",
    sender_name: str = "",
    tracking_url: str | None = None,
) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = str(Address(display_name=sender_name, addr_spec=sender))
    message["To"] = ", ".join(value.to)
    if value.cc:
        message["Cc"] = ", ".join(value.cc)
    message["Subject"] = value.subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = delivery_id
    if value.reference:
        message["X-Galadril-Reference"] = value.reference
    if value.thread_reference:
        message["X-Galadril-Thread-Reference"] = value.thread_reference
    if value.reply_to_message_id:
        message["In-Reply-To"] = value.reply_to_message_id
        message["References"] = (
            value.references + " " + value.reply_to_message_id
        ).strip()
    if automatic:
        message["Auto-Submitted"] = "auto-replied"
        message["X-Auto-Response-Suppress"] = "All"
    fields = {
        "ref": value.thread_reference or value.reference,
        "subject": value.subject,
        "sender": sender,
        "recipient": ", ".join(value.to),
        "date": str(message["Date"]),
    }
    signature, plain = render_signature(signature_html, fields)
    message.set_content(value.body + ("\n\n" + plain if plain else ""))
    if signature_html or tracking_url:
        body_html = escape(value.body).replace("\n", "<br>")
        pixel = (
            '<img src="'
            + escape(tracking_url, quote=True)
            + '" width="1" height="1" alt="" style="display:block;border:0">'
            if tracking_url
            else ""
        )
        message.add_alternative(
            "<div>" + body_html + "</div><br>" + signature + pixel,
            subtype="html",
        )
    for attachment in value.attachments:
        try:
            content = base64.b64decode(attachment.content, validate=True)
            maintype, subtype = attachment.content_type.split("/", 1)
            message.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.name,
            )
        except (ValueError, TypeError):
            raise MailError("Invalid attachment") from None
    raw = message.as_bytes()
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MailError("Outgoing message exceeds size limit")
    return raw
