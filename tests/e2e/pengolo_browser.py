"""Runs the compiled dashboard against the same authorized Gateway and Scribe."""

from authentication import browser_cookie_snapshot, login_user
from browser import run_browser_suite
from clients import OUTSIDER_ID, TENANT_ID, UPLOADER_ID


async def exercise_pengolo_browser(
    prompt: str, search: str, *, administrator: str
) -> None:
    """Keeps browser checks independent of pipeline duration and cookie rotation."""
    for actor in dict.fromkeys((UPLOADER_ID, OUTSIDER_ID, administrator)):
        await login_user(actor, TENANT_ID)
    await run_browser_suite(
        "live.spec.ts",
        {
            "cookies": browser_cookie_snapshot(UPLOADER_ID, TENANT_ID),
            "memberCookies": browser_cookie_snapshot(OUTSIDER_ID, TENANT_ID),
            "administratorCookies": browser_cookie_snapshot(
                administrator, TENANT_ID
            ),
            "memberId": OUTSIDER_ID,
            "prompt": prompt,
            "search": search,
        },
    )
