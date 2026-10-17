"""Generation dependencies keep delegated credentials outside model arguments."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from galadril_scribe.media import StorageMedia
from galadril_scribe.sandbox import Sandbox


@dataclass(slots=True)
class Dependencies:
    client: httpx.AsyncClient
    tools_url: str
    capability: str = field(repr=False)
    sandbox: Sandbox | None
    media: StorageMedia
    public_search: Callable[[str, int], Awaitable[str]] | None = None
    web_remaining: int = 0
    progress: Callable[[str], Awaitable[None]] | None = None
    web_enabled: bool = False
