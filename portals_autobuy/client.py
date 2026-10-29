"""Portals marketplace API client (+ MockClient with offline fixtures).

Verified live on 2026-10-28 (no auth needed):
  GET  {API}/nfts/search?...            -> {"results": [...], "total_count": N}
  GET  {API}/collections?limit=N        -> {"collections": [{id, name, short_name, floor_price, ...}]}
  GET  {API}/collections/filters?short_names=x -> {"collections": {x: {models, backdrops, symbols}}}
  GET  {API}/users/wallets/ without auth -> 401 {"message": "missing auth"}
Host is behind Cloudflare -> curl_cffi with Chrome impersonation (httpx fallback).
Rate limit header on /nfts/search: x-ratelimit-limit: 2 (per second).
Every response carries a Date header (verified); /doctor uses it for the clock-skew check.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote_plus, urlencode

from .config import Settings
from .models import AttributeFloors, CollectionInfo, Listing, norm, short_name
from .rules import BuyRule
from .tg_auth import InitDataProvider

log = logging.getLogger(__name__)

SORT_PRICE_ASC = "price asc"
SORT_LATEST = "listed_at desc"


class PortalsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(PortalsError):
    pass


class RateLimited(PortalsError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after:g}s", 429)
        self.retry_after = retry_after


class UnknownCollection(PortalsError):
    pass


@dataclass(slots=True)
class SearchFilters:
    collections: list[str] = field(default_factory=list)  # display names, resolved to ids
    models: list[str] = field(default_factory=list)
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    sort: str = SORT_PRICE_ASC
    limit: int = 20
    offset: int = 0


class MarketClient(Protocol):
    async def search_listings(self, filters: SearchFilters) -> list[Listing]: ...
    async def collections(self) -> list[CollectionInfo]: ...
    async def collection_floors(self) -> dict[str, float]: ...
    async def collection_meta(self) -> dict[str, CollectionInfo]: ...
    async def attribute_floors(self, collection: str) -> AttributeFloors | None: ...
    async def resolve_collection(self, name: str) -> CollectionInfo: ...
    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None: ...
    async def buy(self, listing: Listing) -> Any: ...
    async def balance(self) -> float: ...
    async def close(self) -> None: ...


BUY_ERROR_KEYS = ("error", "errors", "detail", "message")
BUY_OK_STATUSES = ("ok", "success", "succeeded", "completed", "done")


def classify_buy_response(data: Any) -> tuple[bool, str]:
    """(bought, detail) for a 2xx answer to POST /nfts; a 4xx never gets here (_request raised it).

    Fails closed for the limits: only an explicit success is a buy, that is an object (an empty 2xx body
    reads as {}) with no error/errors/detail/message field, no `success` or `ok` set to false, and a
    `status`, if any, that is a success word or a 2xx code. Anything else (a list, a scalar, an error
    field, another status) may still be a purchase in a shape we do not know, so the caller records it as
    'unknown', which counts toward DAILY_BUDGET_TON and MAX_BUYS_PER_HOUR.
    TODO(verify): the success body of POST /nfts (and whether a 2xx can carry a per-gift failure)."""
    if isinstance(data, dict):
        status = data.get("status", "ok")
        status_ok = (200 <= status < 300) if isinstance(status, int) and not isinstance(status, bool) \
            else str(status).strip().lower() in BUY_OK_STATUSES
        clean = all(data.get(k) in (None, "", [], {}) for k in BUY_ERROR_KEYS)
        if clean and status_ok and data.get("success", True) is not False and data.get("ok", True) is not False:
            ref = data.get("id") or data.get("order_id")
            return True, f"order {ref}" if ref else "accepted"
    shown = json.dumps(data, default=str)[:200]
    return False, f"unrecognized buy response {shown} (result unknown: counted toward your limits, check Portals)"


def find_listing(listings: list[Listing], listing_id: str) -> Listing | None:
    return next((x for x in listings if x.id == listing_id), None)


def recheck_filters(hint: Listing, limit: int) -> SearchFilters:
    """Single-listing re-check before a confirmed buy: the listing's collection and model, cheapest first.
    TODO(verify): no single-NFT endpoint is known; a listing past the first page reads as gone."""
    return SearchFilters(collections=[hint.collection], models=[hint.model] if hint.model else [],
                         sort=SORT_PRICE_ASC, limit=max(limit, 20))


def _fmt_num(v: float) -> str:
    return f"{v:.9f}".rstrip("0").rstrip(".")


def build_search_params(filters: SearchFilters, collection_ids: list[str]) -> list[tuple[str, str]]:
    """Query params for GET /nfts/search (order mirrors the web app)."""
    params: list[tuple[str, str]] = [
        ("offset", str(filters.offset)),
        ("limit", str(filters.limit)),
        ("sort_by", filters.sort),
    ]
    if collection_ids:
        # NOTE: filter_by_collections=<name> is silently ignored by the API (checked live);
        # collection_ids=<uuid> works.
        params.append(("collection_ids", ",".join(collection_ids)))
    for key, values in (
        ("filter_by_models", filters.models),
        ("filter_by_backdrops", filters.backdrops),
        ("filter_by_symbols", filters.symbols),
    ):
        if values:
            params.append((key, ",".join(values)))
    if filters.min_price is not None or filters.max_price is not None:
        params.append(("min_price", _fmt_num(filters.min_price or 0)))
        params.append(("max_price", _fmt_num(filters.max_price or 100000)))
    # TODO(verify): whether premarket / locked (`unlocks_at`) gifts should be sniped at all.
    params += [("status", "listed"), ("exclude_bundled", "true"), ("premarket_status", "all")]
    return params


class _Http:
    """Thin async HTTP wrapper: curl_cffi (Chrome TLS fingerprint) if installed, else httpx."""

    def __init__(self, timeout: float, proxy: str = ""):
        try:
            from curl_cffi.requests import AsyncSession

            self.kind = "curl_cffi"
            proxies = {"http": proxy, "https": proxy} if proxy else None
            self._s: Any = AsyncSession(impersonate="chrome", timeout=timeout, proxies=proxies)
        except ImportError:  # pragma: no cover - depends on environment
            import httpx

            self.kind = "httpx"
            self._s = httpx.AsyncClient(timeout=timeout, proxy=proxy or None)

    async def request(
        self, method: str, url: str, headers: dict[str, str], body: Any = None
    ) -> tuple[int, dict[str, str], str]:
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        r = await self._s.request(method, url, **kwargs)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.text

    async def close(self) -> None:
        if self.kind == "curl_cffi":
            await self._s.close()
        else:
            await self._s.aclose()


class PortalsClient:
    name = "portals"

    def __init__(self, settings: Settings, auth: InitDataProvider | None = None):
        self.settings = settings
        self.base = settings.PORTALS_API_URL.rstrip("/")
        self.auth = auth or InitDataProvider(settings)
        self._http = _Http(settings.HTTP_TIMEOUT, settings.HTTP_PROXY)
        origin = settings.PORTALS_WEBAPP_URL.rstrip("/")
        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": origin,
            "Referer": origin + "/",
        }
        if self._http.kind == "httpx":
            self._headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            )
        self._min_interval = 1.0 / max(settings.REQUESTS_PER_SECOND, 0.1)
        self._next_slot = 0.0
        self._throttle = asyncio.Lock()
        self._collections: list[CollectionInfo] = []
        self._collections_at = 0.0
        self._attr_cache: dict[str, tuple[float, AttributeFloors | None]] = {}
        # Clock-skew and latency info for /doctor (Date header of the last response).
        self.last_server_date: float | None = None
        self.last_response_at: float | None = None
        self.last_latency_ms: float | None = None
        self.last_frozen: float | None = None  # frozen_funds of the last balance() call, when sent

    async def close(self) -> None:
        await self._http.close()

    # ---------- transport ----------
    async def _wait_slot(self) -> None:
        async with self._throttle:
            now = time.monotonic()
            if self._next_slot > now:
                await asyncio.sleep(self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._min_interval

    async def _request(
        self,
        method: str,
        path: str,
        params: list[tuple[str, str]] | None = None,
        body: Any = None,
        auth: bool = False,
    ) -> Any:
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params, quote_via=quote_plus)
        auth_retry = rate_retry = False
        while True:
            headers = dict(self._headers)
            if auth:
                init_data = await self.auth.get(force=auth_retry)
                headers["Authorization"] = f"tma {init_data}"
            await self._wait_slot()
            started = time.perf_counter()
            try:
                status, rh, text = await self._http.request(method, url, headers, body)
            except Exception as exc:  # network / TLS / timeout
                raise PortalsError(f"{method} {path}: network error: {exc}") from exc
            self._note_response(rh, started)
            if status in (401, 403) and auth and not auth_retry:
                log.warning("%s %s -> %s, refreshing initData", method, path, status)
                auth_retry = True
                continue
            if status == 429:
                retry = rh.get("retry-after") or rh.get("x-ratelimit-reset") or "1"
                try:
                    retry_after = max(float(retry), 0.5)
                except ValueError:
                    retry_after = 1.0
                if not rate_retry and retry_after <= 5:
                    log.debug("%s %s -> 429, retrying in %.1fs", method, path, retry_after)
                    rate_retry = True
                    await asyncio.sleep(retry_after)
                    continue
                raise RateLimited(retry_after)
            if status in (401, 403):
                raise AuthError(f"{method} {path}: HTTP {status}: {_short(text)}", status)
            if not 200 <= status < 300:
                raise PortalsError(f"{method} {path}: HTTP {status}: {_short(text)}", status)
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except ValueError as exc:
                raise PortalsError(f"{method} {path}: non-JSON response: {_short(text)}", status) from exc

    def _note_response(self, headers: dict[str, str], started: float) -> None:
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        self.last_response_at = time.time()
        try:
            date = headers.get("date")
            self.last_server_date = parsedate_to_datetime(date).timestamp() if date else None
        except (TypeError, ValueError):
            self.last_server_date = None

    # ---------- collections / floors ----------
    async def collections(self) -> list[CollectionInfo]:
        if self._collections and time.monotonic() - self._collections_at < self.settings.FLOORS_TTL:
            return self._collections
        # TODO(verify): no pagination seen (123 collections returned with limit=500).
        data = await self._request("GET", "/collections", [("limit", "500")])
        self._collections = [CollectionInfo.from_api(c) for c in data.get("collections") or []]
        self._collections_at = time.monotonic()
        return self._collections

    async def resolve_collection(self, name: str) -> CollectionInfo:
        key = short_name(name)
        for c in await self.collections():
            if c.short_name == key or short_name(c.name) == key:
                return c
        raise UnknownCollection(f"unknown collection: {name!r}")

    async def collection_floors(self) -> dict[str, float]:
        """{short_name: floor_ton} for every collection."""
        return {c.short_name: c.floor_ton for c in await self.collections() if c.floor_ton}

    async def collection_meta(self) -> dict[str, CollectionInfo]:
        """{short_name: CollectionInfo} incl. zero_commission (public /collections; same cached call as floors)."""
        return {c.short_name: c for c in await self.collections()}

    async def attribute_floors(self, collection: str) -> AttributeFloors | None:
        key = short_name(collection)
        hit = self._attr_cache.get(key)
        if hit and time.monotonic() - hit[0] < self.settings.FLOORS_TTL:
            return hit[1]
        data = await self._request("GET", "/collections/filters", [("short_names", key)])
        raw = (data.get("collections") or {}).get(key)
        value = AttributeFloors.from_api(raw) if isinstance(raw, dict) else None
        self._attr_cache[key] = (time.monotonic(), value)
        return value

    # ---------- listings ----------
    async def search_listings(self, filters: SearchFilters) -> list[Listing]:
        ids = [(await self.resolve_collection(n)).id for n in filters.collections]
        data = await self._request("GET", "/nfts/search", build_search_params(filters, ids))
        out = []
        for item in data.get("results") or []:
            try:
                out.append(Listing.from_api(item))
            except (KeyError, TypeError) as exc:
                log.debug("skip unparsable listing: %s", exc)
        return out

    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None:
        """Re-fetch one listing before a confirmed buy (same id, still for sale, current price)."""
        if hint is None:
            return None
        return find_listing(await self.search_listings(recheck_filters(hint, self.settings.SEARCH_LIMIT)), listing_id)

    # ---------- account ----------
    async def buy(self, listing: Listing) -> Any:
        """The raw 2xx body; the engine reads it with classify_buy_response (only an explicit success is a buy)."""
        # Payload confirmed in aportalsmp source; response shape unknown.
        # TODO(verify): response body / error codes (sold, price changed, insufficient funds).
        body = {"nft_details": [{"id": listing.id, "price": listing.price_str}]}
        return await self._request("POST", "/nfts", body=body, auth=True)

    async def balance(self) -> float:
        # TODO(verify): whether the marketplace fee is on top of the listing price (balance check uses price).
        # TODO(verify): response shape {"balance": "...", "frozen_funds": "..."} (from aportalsmp).
        # TODO(verify): Portals may freeze funds (offers) separately; the balance-mismatch breaker compares
        # available + frozen when frozen_funds is present.
        data = await self._request("GET", "/users/wallets/", auth=True)
        frozen = data.get("frozen_funds")
        try:
            self.last_frozen = float(frozen) if frozen not in (None, "") else None
        except (TypeError, ValueError):
            self.last_frozen = None
        return float(data.get("balance") or 0)


def _short(text: str, n: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "..."


# ======================================================================
# Mock client
# ======================================================================

def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _mock_item(
    nft_id: str, coll: dict[str, Any], number: int, price: str,
    model: str, backdrop: str, symbol: str, minutes_ago: float, **extra: Any,
) -> dict[str, Any]:
    listed = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    item = {
        "id": nft_id,
        "bundle_id": None,
        "tg_id": "".join(ch for ch in coll["name"] if ch.isalnum()) + f"-{number}",
        "collection_id": coll["id"],
        "external_collection_number": number,
        "name": coll["name"],
        "price": price,
        "attributes": [
            {"type": "model", "value": model, "rarity_per_mille": 10},
            {"type": "symbol", "value": symbol, "rarity_per_mille": 5},
            {"type": "backdrop", "value": backdrop, "rarity_per_mille": 15},
        ],
        "listed_at": _iso(listed),
        "status": "listed",
        "floor_price": coll["floor_price"],
        "unlocks_at": None,
        "lucky_buy_eligible": False,
    }
    item.update(extra)
    return item


# --- demo scenario: the named fixtures F1-F8 used by `python -m portals_autobuy mock` ---------------

DEMO_COLLECTIONS = [
    {"id": "demo-lunarsnake", "name": "Lunar Snake", "short_name": "lunarsnake", "floor_price": "0.97",
     "zero_commission": False, "listed_count": 2140, "supply": 300000},
    {"id": "demo-deskcalendar", "name": "Desk Calendar", "short_name": "deskcalendar", "floor_price": "2.2",
     "zero_commission": False, "listed_count": 950},
    # zero_commission (public flag on /api/collections) -> 0% sell fee for this collection.
    {"id": "demo-jellybunny", "name": "Jelly Bunny", "short_name": "jellybunny", "floor_price": "1.1",
     "zero_commission": True, "listed_count": 410},
    {"id": "demo-spyagaric", "name": "Spy Agaric", "short_name": "spyagaric", "floor_price": "5",
     "zero_commission": False, "listed_count": 120},
    {"id": "demo-homemadecake", "name": "Homemade Cake", "short_name": "homemadecake", "floor_price": "3",
     "zero_commission": False, "listed_count": 760},
    {"id": "demo-plushpepe", "name": "Plush Pepe", "short_name": "plushpepe", "floor_price": "4200",
     "zero_commission": False, "listed_count": 7},
]


def _rarity(item: dict[str, Any], model: float, backdrop: float, symbol: float) -> dict[str, Any]:
    for attr in item["attributes"]:
        attr["rarity_per_mille"] = {"model": model, "backdrop": backdrop, "symbol": symbol}[attr["type"]]
    return item


def demo_listings(now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(initial book, listings revealed by advance()). `now` positions the unlock date of the locked gift."""
    ls, dc, jb, sa, hc, _ = DEMO_COLLECTIONS
    unlock = _iso(datetime.fromtimestamp(now + 6 * 86400 - 3600, tz=timezone.utc))
    g = _mock_item
    initial = [
        # F1 'troll ask': one model at 1.00, 3.00, 1.60, 1.62, 1.65 -> cluster floor 1.60, not 3.00.
        _rarity(g("ls-4410", ls, 4410, "1", "Albino", "Onyx Black", "Star", 3), 15, 20, 8),
        g("ls-4411", ls, 4411, "3", "Albino", "Ivory White", "Moon", 9),
        g("ls-4413", ls, 4413, "1.6", "Albino", "Mint Green", "Leaf", 40),
        g("ls-4414", ls, 4414, "1.62", "Albino", "Sky Blue", "Pin", 50),
        g("ls-4415", ls, 4415, "1.65", "Albino", "Coral Red", "Clip", 60),
        # F2 'loses after fees': 0.97 against a cluster floor of 1.00 (5% sell fee + 0.1 TON withdraw).
        g("ls-4412", ls, 4412, "0.97", "Cobra", "Onyx Black", "Moon", 4),
        g("ls-4420", ls, 4420, "1", "Cobra", "Ivory White", "Star", 70),
        g("ls-4421", ls, 4421, "1.02", "Cobra", "Mint Green", "Leaf", 80),
        g("ls-4422", ls, 4422, "1.05", "Cobra", "Sky Blue", "Pin", 90),
        # Confirm-mode deal (repriced before the tap) and a near miss over the rule's max.
        g("dc-3002", dc, 3002, "2.2", "Classic", "Sky Blue", "Pin", 5),
        g("dc-3001", dc, 3001, "2.62", "Classic", "Coral Red", "Clip", 6),
        *[g(f"dc-30{10 + i}", dc, 3010 + i, f"{p:g}", "Retro", "Onyx Black", "Star", 100 + i)
          for i, p in enumerate((3.20, 3.25, 3.30, 3.35, 3.40))],
        # Watch-mode deal (zero_commission collection), F3 'locked' (unlocks_at) and F5 'trap' (bundle_id).
        g("jb-55", jb, 55, "1.5", "Pinky", "Ivory White", "Moon", 7),
        g("jb-50", jb, 50, "2", "Pinky", "Onyx Black", "Star", 110),
        g("jb-51", jb, 51, "2.05", "Pinky", "Mint Green", "Leaf", 120),
        g("jb-52", jb, 52, "2.1", "Pinky", "Sky Blue", "Pin", 130),
        g("jb-53", jb, 53, "1.2", "Pinky", "Coral Red", "Clip", 8, unlocks_at=unlock),
        g("jb-54", jb, 54, "1.1", "Pinky", "Onyx Black", "Moon", 9, bundle_id="bundle-7f3a"),
        # F7 'cheaper elsewhere': 5.00 here, 4.60 all-in on MRKT (fixture snapshot).
        g("sa-777", sa, 777, "5", "Amanita", "Onyx Black", "Star", 10),
        *[g(f"sa-78{i}", sa, 780 + i, f"{p:g}", "Amanita", "Ivory White", "Moon", 140 + i)
          for i, p in enumerate((6.00, 6.10, 6.20, 6.30, 6.40))],
        # F8 'thin floor above Telegram': one comparable only; Telegram's average is lower.
        g("hc-1234", hc, 1234, "3", "Choco", "Sky Blue", "Pin", 11),
        g("hc-1240", hc, 1240, "4", "Choco", "Mint Green", "Leaf", 150),
    ]
    later = [
        # F4 'too good': 10% of the floor -> alert only, never auto-bought.
        g("ls-4416", ls, 4416, "0.16", "Albino", "Onyx Black", "Moon", 0.5),
        # F6 'healthy deal': ends in a practice buy.
        g("ls-4417", ls, 4417, "1.2", "Albino", "Mint Green", "Star", 0.2),
    ]
    return initial, later


DEMO_RULES = [
    BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, min_profit_ton=0.1, source="demo"),
    BuyRule("Lunar Snake", 0.99, model="Cobra", source="demo"),
    BuyRule("Desk Calendar", 2.5, min_discount_pct=15, mode="confirm", source="demo"),
    BuyRule("Jelly Bunny", 5.0, min_discount_pct=10, mode="watch", source="demo"),
    BuyRule("Spy Agaric", 5.5, source="demo"),
    BuyRule("Homemade Cake", 3.5, min_confidence="low", source="demo"),
]


class MockClient:
    """Offline stand-in for PortalsClient. Applies filters/sort to the demo fixture book (F1-F8; call
    advance() to reveal the later listings). `requests` counts what would be HTTP requests
    on the real client, with the same FLOORS_TTL caching of /collections and /collections/filters."""

    name = "mock"

    def __init__(self, balance_ton: float = 100.0, *, now: float | None = None, floors_ttl: float = 60.0):
        initial, later = demo_listings(time.time() if now is None else now)
        self._items = copy.deepcopy(initial)
        self._later = copy.deepcopy(later)
        self._collections_raw = DEMO_COLLECTIONS
        self._balance = balance_ton
        self.bought: list[str] = []
        self.calls: list[str] = []
        self.requests = 0
        self.fail_lookups = False
        self.floors_ttl = floors_ttl
        self._coll_at = -1e18
        self._attr_at: dict[str, float] = {}
        # /doctor reads these like on the real client.
        self.last_server_date: float | None = None
        self.last_response_at: float | None = None
        self.last_latency_ms: float | None = 120.0
        self.last_frozen: float | None = None

    def advance(self) -> None:
        """Demo: reveal the later fixture listings (F4, F6)."""
        self._items.extend(self._later)
        self._later = []

    def reprice(self, listing_id: str, price_ton: float) -> None:
        for raw in self._items:
            if raw["id"] == listing_id:
                raw["price"] = f"{price_ton:g}"

    def remove(self, listing_id: str) -> None:
        self._items = [r for r in self._items if r["id"] != listing_id]

    def crash(self, collection: str, model: str | None, factor: float) -> None:
        """Move every listing of a collection/model by `factor` (mock --crash)."""
        for raw in self._items:
            lst = Listing.from_api(raw)
            if norm(lst.collection) == norm(collection) and (not model or norm(lst.model) == norm(model)):
                raw["price"] = f"{round(lst.price_ton * factor, 4):g}"

    async def close(self) -> None:
        return None

    def _http(self, what: str) -> None:
        self.requests += 1
        self.last_response_at = time.time()

    async def collections(self) -> list[CollectionInfo]:
        if time.monotonic() - self._coll_at >= self.floors_ttl:
            self._http("collections")
            self._coll_at = time.monotonic()
        return [CollectionInfo.from_api(c) for c in self._collections_raw]

    async def resolve_collection(self, name: str) -> CollectionInfo:
        key = short_name(name)
        for c in await self.collections():
            if c.short_name == key:
                return c
        raise UnknownCollection(f"unknown collection: {name!r}")

    async def collection_floors(self) -> dict[str, float]:
        return {c.short_name: c.floor_ton for c in await self.collections() if c.floor_ton}

    async def collection_meta(self) -> dict[str, CollectionInfo]:
        return {c.short_name: c for c in await self.collections()}

    async def attribute_floors(self, collection: str) -> AttributeFloors | None:
        key = short_name(collection)
        if time.monotonic() - self._attr_at.get(key, -1e18) >= self.floors_ttl:
            self._http(f"filters:{key}")
            self._attr_at[key] = time.monotonic()
        tables: dict[str, dict[str, float]] = {"model": {}, "backdrop": {}, "symbol": {}}
        found = False
        for lst in (Listing.from_api(i) for i in self._items):
            if short_name(lst.collection) != key:
                continue
            found = True
            for kind in tables:
                name = getattr(lst, kind)
                if name:
                    cur = tables[kind].get(name)
                    tables[kind][name] = lst.price_ton if cur is None else min(cur, lst.price_ton)
        if not found:
            return None
        return AttributeFloors(models=tables["model"], backdrops=tables["backdrop"], symbols=tables["symbol"])

    async def search_listings(self, filters: SearchFilters) -> list[Listing]:
        self._http("search")
        self.calls.append(f"search:{filters.sort}:{','.join(filters.collections)}")
        colls = {short_name(c) for c in filters.collections}
        res = []
        for lst in (Listing.from_api(i) for i in self._items):
            if colls and short_name(lst.collection) not in colls:
                continue
            if filters.models and lst.model not in filters.models:
                continue
            if filters.backdrops and lst.backdrop not in filters.backdrops:
                continue
            if filters.symbols and lst.symbol not in filters.symbols:
                continue
            if filters.min_price is not None and lst.price_ton < filters.min_price:
                continue
            if filters.max_price is not None and lst.price_ton > filters.max_price:
                continue
            res.append(lst)
        if filters.sort == SORT_LATEST:
            epoch = datetime.min.replace(tzinfo=timezone.utc)
            res.sort(key=lambda x: x.listed_at or epoch, reverse=True)
        else:
            res.sort(key=lambda x: x.price_ton)
        return res[filters.offset : filters.offset + filters.limit]

    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None:
        self._http("get_listing")
        if self.fail_lookups:
            raise PortalsError("GET /nfts/search: network error: mock lookup failure")
        raw = next((r for r in self._items if r["id"] == listing_id), None)
        return Listing.from_api(raw) if raw else None

    async def buy(self, listing: Listing) -> Any:
        self._http("buy")
        self.calls.append(f"buy:{listing.id}")
        if listing.price_ton > self._balance:
            raise PortalsError("insufficient balance (mock)", 400)
        self._balance -= listing.price_ton
        self._items = [i for i in self._items if i["id"] != listing.id]
        self.bought.append(listing.id)
        return {"mock": True, "id": listing.id}

    async def balance(self) -> float:
        self._http("balance")
        return self._balance
