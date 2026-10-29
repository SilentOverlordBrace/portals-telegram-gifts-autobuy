"""Telegram Mini App initData for Portals via a Telethon user session (also used for Telegram's own
gift valuation and the /hunt link lookup).

The Portals API expects `Authorization: tma <initData>`. initData is obtained by opening
the @portals Mini App from our own user account (RequestAppWebView with short name
"market", falling back to RequestWebView with the site URL) and reading `tgWebAppData`
from the returned URL fragment.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import unquote, urlsplit

from .config import ConfigError, Settings

log = logging.getLogger(__name__)


class TgAuthError(RuntimeError):
    pass


def mask(secret: str | None, keep: int = 12) -> str:
    """Never log full secrets."""
    if not secret:
        return "<empty>"
    return secret[:keep] + f"...({len(secret)} chars)" if len(secret) > keep else "***"


def extract_init_data(url: str) -> str:
    """Pull `tgWebAppData` out of a Mini App URL (fragment first, then query) and unquote it."""
    parts = urlsplit(url)
    for chunk in (parts.fragment, parts.query):
        for item in chunk.split("&"):
            if item.startswith("tgWebAppData="):
                value = unquote(item[len("tgWebAppData="):])
                if value:
                    return value
    raise TgAuthError("tgWebAppData not found in web view URL")


class InitDataProvider:
    """Caches initData; refreshes after INIT_DATA_TTL or when forced (on 401/403)."""

    def __init__(self, settings: Settings, session: "TelegramSession | None" = None):
        self.settings = settings
        self.session = session
        self._value: str | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def age(self) -> float | None:
        """Seconds since initData was fetched (None before the first fetch or with static PORTALS_INIT_DATA)."""
        return None if self._value is None else time.monotonic() - self._fetched_at

    async def get(self, force: bool = False) -> str:
        if self.settings.PORTALS_INIT_DATA:
            if force:
                log.warning("PORTALS_INIT_DATA is static and was rejected; refresh it manually")
            return self.settings.PORTALS_INIT_DATA.removeprefix("tma ").strip()
        async with self._lock:
            fresh = time.monotonic() - self._fetched_at < self.settings.INIT_DATA_TTL
            if self._value and fresh and not force:
                return self._value
            self._value = await fetch_init_data(self.settings, self.session)
            self._fetched_at = time.monotonic()
            log.info("Got Portals initData %s", mask(self._value))
            return self._value


def _client(settings: Settings):
    settings.require("TG_API_ID", "TG_API_HASH")
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("telethon is not installed: pip install -r requirements.txt") from exc
    return TelegramClient(str(settings.session_path), settings.TG_API_ID, settings.TG_API_HASH)


class TelegramSession:
    """One shared, lazily connected Telethon client (initData, Telegram's gift valuation, /hunt link lookup).

    Sharing avoids two clients locking the same .session SQLite file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    @property
    def configured(self) -> bool:
        return self.settings.has_tg_credentials

    async def client(self):
        if self._client is None:
            self._client = _client(self.settings)
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise TgAuthError("Telegram session is not authorized. Run: python -m portals_autobuy login")
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


async def probe_session(settings: Settings, session: "TelegramSession | None" = None, timeout: float = 5.0):
    """(authorized, has_password) for /doctor. Never logs or returns the phone number."""
    from telethon.tl.functions.account import GetPasswordRequest

    own = session is None
    session = session or TelegramSession(settings)
    try:
        try:
            client = await asyncio.wait_for(session.client(), timeout)
        except (TgAuthError, ConfigError):
            return False, None
        pwd = await asyncio.wait_for(client(GetPasswordRequest()), timeout)
        return True, bool(getattr(pwd, "has_password", False))
    finally:
        if own:
            await session.close()


async def lookup_gift(session: "TelegramSession", slug: str, timeout: float = 5.0) -> dict | None:
    """payments.getUniqueStarGift(slug) -> {collection, number, model, backdrop, symbol} for the /hunt wizard."""
    from telethon.tl.functions.payments import GetUniqueStarGiftRequest

    client = await session.client()
    res = await asyncio.wait_for(client(GetUniqueStarGiftRequest(slug=slug)), timeout)
    gift = getattr(res, "gift", None)
    if gift is None:
        return None
    out = {"collection": getattr(gift, "title", None), "number": getattr(gift, "num", None)}
    for attr in getattr(gift, "attributes", []) or []:
        kind = type(attr).__name__  # StarGiftAttributeModel / Backdrop / Pattern
        name = getattr(attr, "name", None)
        if kind.endswith("Model"):
            out["model"] = name
        elif kind.endswith("Backdrop"):
            out["backdrop"] = name
        elif kind.endswith("Pattern"):
            out["symbol"] = name
    return out


async def fetch_init_data(settings: Settings, session: "TelegramSession | None" = None) -> str:
    if session is not None:
        return await _request_init_data(settings, await session.client())
    client = _client(settings)
    await client.connect()
    try:
        return await _request_init_data(settings, client)
    finally:
        await client.disconnect()


async def _request_init_data(settings: Settings, client) -> str:
    from telethon import functions, types, utils

    if not await client.is_user_authorized():
        raise TgAuthError(
            "Telegram session is not authorized. Run: python -m portals_autobuy login"
        )
    bot = await client.get_input_entity(settings.PORTALS_BOT_USERNAME)
    bot_user = utils.get_input_user(bot)
    try:
        res = await client(
            functions.messages.RequestAppWebViewRequest(
                peer=bot,
                app=types.InputBotAppShortName(
                    bot_id=bot_user, short_name=settings.PORTALS_APP_SHORT_NAME
                ),
                platform="android",
            )
        )
    except Exception as exc:  # TODO(verify): short name "market" taken from aportalsmp
        log.info("RequestAppWebView failed (%s), falling back to RequestWebView", exc)
        res = await client(
            functions.messages.RequestWebViewRequest(
                peer=bot,
                bot=bot_user,
                platform="android",
                from_bot_menu=False,
                url=settings.PORTALS_WEBAPP_URL,
            )
        )
    return extract_init_data(res.url)


async def interactive_login(settings: Settings) -> str:
    """First-time login: prompts for phone / code / 2FA password in the terminal."""
    client = _client(settings)
    await client.start()  # interactive prompts
    me = await client.get_me()
    await client.disconnect()
    session_file = settings.session_path.with_name(settings.session_path.name + ".session")
    if session_file.exists():
        session_file.chmod(0o600)  # /doctor checks this
    name = getattr(me, "username", None) or getattr(me, "first_name", "?")
    return f"Logged in as {name} (id={getattr(me, 'id', '?')}); session: {settings.session_path}.session"
