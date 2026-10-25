"""Render bounded Markdown signatures without granting embedded HTML authority."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from galadril_plugins.utils.models import SIGNATURE_FIELD
from galadril_plugins.utils.store import MailError
from markdown_it import MarkdownIt
from markdown_it.token import Token

_MARKDOWN = MarkdownIt("commonmark", {"html": True, "breaks": True})


def _line_break(
    tokens: Sequence[Token], index: int, _options: object, _environment: object
) -> str:
    """An explicit HTML break already carries the following source newline."""
    previous = tokens[index - 1] if index > 0 else None
    if (
        previous is not None
        and previous.type == "html_inline"
        and re.search(r"<br\s*/?>\s*$", previous.content, re.IGNORECASE)
    ):
        return ""
    return "<br>"


_MARKDOWN.renderer.rules.update(
    {"softbreak": _line_break, "hardbreak": _line_break}
)


def image_source(value: str) -> str | None:
    """Images are client-fetched HTTPS resources, never server-side URL fetches."""
    if (
        len(value) > 2048
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        host = (
            (parsed.hostname or "")
            .encode("idna")
            .decode("ascii")
            .lower()
            .rstrip(".")
        )
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return None
    except (ValueError, UnicodeError):
        return None
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
        or not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1])
        or host.endswith((".local", ".localhost", ".internal", ".home.arpa"))
    ):
        return None
    return urlunsplit(
        ("https", host, parsed.path or "/", parsed.query, parsed.fragment)
    )


class SignatureParser(HTMLParser):
    """Keep email-safe markup and preserve raw HTML line breaks explicitly."""

    __slots__ = ("parts", "plain", "blocked", "stack", "after_break")
    tags = frozenset(
        {
            "p",
            "br",
            "b",
            "strong",
            "i",
            "em",
            "u",
            "div",
            "span",
            "a",
            "ul",
            "ol",
            "li",
            "code",
            "pre",
            "small",
            "img",
            "blockquote",
            "hr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }
    )
    blocked_tags = frozenset(
        {"script", "style", "iframe", "object", "svg", "math"}
    )
    void = frozenset({"br", "img", "hr"})
    containers = tags - {"ul", "ol", "br", "img", "hr", "pre", "code"}
    blocks = frozenset(
        {
            "p",
            "div",
            "li",
            "blockquote",
            "pre",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.plain: list[str] = []
        self.stack: list[str] = []
        self.blocked = 0
        self.after_break = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in self.blocked_tags:
            self.blocked += 1
        if self.blocked or tag not in self.tags:
            return
        attributes = ""
        values = dict(attrs)
        if tag == "a":
            href = values.get("href")
            if href and href.lower().startswith(
                ("https://", "http://", "mailto:")
            ):
                attributes = (
                    ' href="'
                    + escape(href, quote=True)
                    + '" rel="noreferrer noopener"'
                )
        elif tag == "img":
            source = image_source(values.get("src") or "")
            if source is None:
                return
            alt = (values.get("alt") or "")[:512]
            attributes = (
                ' src="'
                + escape(source, quote=True)
                + '" alt="'
                + escape(alt, quote=True)
                + '" referrerpolicy="no-referrer"'
            )
            for name in ("width", "height"):
                value = values.get(name) or ""
                if (
                    re.fullmatch(r"[0-9]{1,4}", value)
                    and 0 < int(value) <= 1024
                ):
                    attributes += f' {name}="{int(value)}"'
            self.plain.append(alt)
        elif tag == "ol":
            start = values.get("start") or ""
            if re.fullmatch(r"[0-9]{1,4}", start):
                attributes = f' start="{int(start)}"'
        self.parts.append("<" + tag + attributes + ">")
        if tag not in self.void:
            self.stack.append(tag)
        if tag in self.blocks or tag in {"br", "hr"}:
            self.plain.append("\n")
        self.after_break = tag == "br"

    def handle_endtag(self, tag: str) -> None:
        if tag in self.blocked_tags:
            self.blocked = max(0, self.blocked - 1)
            return
        if self.blocked or tag not in self.tags or tag in self.void:
            return
        self.parts.append("</" + tag + ">")
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                continue
        if tag in self.blocks:
            self.plain.append("\n")
        self.after_break = False

    def handle_data(self, data: str) -> None:
        if self.blocked:
            return
        if "pre" in self.stack or "code" in self.stack:
            self.parts.append(escape(data))
            self.plain.append(data)
            return
        if self.after_break and data.startswith("\n"):
            data = data[1:]
        self.after_break = False
        if not data or (
            data.isspace()
            and not any(tag in self.containers for tag in self.stack)
        ):
            return
        self.parts.append(escape(data).replace("\n", "<br>"))
        self.plain.append(data)


def render_signature(
    template: str, fields: Mapping[str, str]
) -> tuple[str, str]:
    """Substitute metadata after Markdown parsing to prevent Markdown injection."""
    if len(template) > 8192:
        raise MailError("Signature exceeds size limit")
    rendered = _MARKDOWN.render(
        template.replace("\r\n", "\n").replace("\r", "\n")
    )
    size = len(rendered)

    def substitute(match: re.Match[str]) -> str:
        nonlocal size
        value = fields.get(match.group(1))
        if value is None:
            raise MailError("Unknown signature field")
        text = escape(value, quote=True)
        size += len(text) - len(match.group(0))
        if size > 65536:
            raise MailError("Expanded signature exceeds size limit")
        return text

    if size > 65536:
        raise MailError("Expanded signature exceeds size limit")
    parser = SignatureParser()
    parser.feed(SIGNATURE_FIELD.sub(substitute, rendered))
    parser.close()
    return "".join(parser.parts), "".join(parser.plain).strip()
