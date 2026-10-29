"""Shared offline test helpers (no network, no credentials)."""

from __future__ import annotations

from dataclasses import replace

from portals_autobuy.client import DEMO_RULES, MockClient
from portals_autobuy.config import Settings
from portals_autobuy.engine import Engine
from portals_autobuy.models import Listing
from portals_autobuy.notifier import MemoryNotifier
from portals_autobuy.storage import Storage

ADMIN = 42
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def settings(**overrides) -> Settings:
    base = dict(_env_file=None, DRY_RUN=True, MAX_PRICE_TON=10, DAILY_BUDGET_TON=50, MAX_BUYS_PER_HOUR=3,
                POLL_INTERVAL=0.01, SEARCH_LIMIT=20, ADMIN_IDS=str(ADMIN))
    base.update(overrides)
    return Settings(**base)


def L(lid: str, price: float, collection: str = "Lunar Snake", model: str | None = "Albino", number: int | None = 1,
      **raw) -> Listing:
    """A listing as Listing.from_api builds it: the price came from Portals' `price` field (decimal TON)."""
    raw.setdefault("price", f"{price:g}")
    return Listing(id=lid, collection=collection, model=model, backdrop=None, symbol=None, number=number,
                   price_ton=price, listed_at=None, raw=raw)


async def demo_engine(s: Settings | None = None, *, rules=None, force_dry: bool = True, clock: Clock | None = None,
                      client: MockClient | None = None, **kw):
    s = s or settings()
    clock = clock or Clock()
    storage = await Storage(":memory:").open()
    for rule in (DEMO_RULES if rules is None else rules):
        await storage.add_rule(replace(rule, id=None))
    client = client or MockClient(now=clock())
    notifier = MemoryNotifier()
    engine = Engine(s, storage, client, notifier, force_dry=force_dry, clock=clock, **kw)
    return engine, storage, client, notifier, clock
