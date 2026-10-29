import asyncio

import pytest
from helpers import demo_engine

from portals_autobuy.client import MockClient
from portals_autobuy.config import Settings
from portals_autobuy.engine import Engine, Limits, backoff_delay, check_limits_detail
from portals_autobuy.notifier import Notifier
from portals_autobuy.storage import Storage


def test_check_limits():
    limits = Limits(max_price_ton=10, daily_budget_ton=20, max_buys_per_hour=2)

    def hit(cost, spent=0.0, buys=0):
        found = check_limits_detail(cost, limits, spent_24h=spent, buys_last_hour=buys)
        return found[1] if found else None

    assert hit(5) is None
    assert "MAX_PRICE_TON" in hit(11)
    assert "daily budget" in hit(5, spent=16)
    assert "MAX_BUYS_PER_HOUR" in hit(5, buys=2)


def test_backoff_delay():
    assert backoff_delay(0, 2, 60) == 2
    assert backoff_delay(1, 2, 60) == 4
    assert backoff_delay(3, 2, 60) == 16
    assert backoff_delay(10, 2, 60) == 60


def settings(**kw):
    base = dict(DRY_RUN=True, MAX_PRICE_TON=10, DAILY_BUDGET_TON=30, MAX_BUYS_PER_HOUR=3, BOT_TOKEN="")
    base.update(kw)
    return Settings(_env_file=None, **base)


async def _pipeline(s, *, force_dry, cycles=2):
    engine, storage, client, notifier, _ = await demo_engine(s, force_dry=force_dry)
    if not force_dry and not s.DRY_RUN:
        assert await engine.arm_live() is None  # LIVE only ever exists as an armed, expiring gate
    reports = []
    for i in range(cycles):
        if i:
            client.advance()  # the demo book reveals two fresh listings on cycle 2
        reports.append(await engine.run_cycle())
    purchases = await storage.recent_purchases(50)
    await storage.close()
    return reports, purchases, notifier, client


def test_mock_pipeline_dry():
    reports, purchases, notifier, client = asyncio.run(_pipeline(settings(), force_dry=True))
    r1, r2 = reports
    assert r1.matched >= 1 and r1.dry_bought >= 1 and r1.rejected >= 1
    assert r2.new == 2  # dedupe: only the two fresh listings are new on cycle 2
    assert [(p.status, p.nft_id, p.dry) for p in reversed(purchases)][0] == ("dry", "ls-4410", True)
    assert purchases[-1].floor_ton == pytest.approx(1.60)
    assert any(t.startswith("DEAL Lunar Snake #4410") and "PRACTICE: would buy" in t for t in notifier.sent)
    assert not any(t.startswith("DRY:") for t in notifier.sent)  # one card per practice buy, no second message
    assert not client.bought  # practice never calls buy()


def test_mock_pipeline_live_buys_through_client():
    reports, purchases, _, client = asyncio.run(_pipeline(settings(DRY_RUN=False), force_dry=False, cycles=1))
    assert reports[0].bought >= 1
    assert client.bought[0] == "ls-4410"
    ok = [p for p in purchases if p.nft_id == "ls-4410"]
    assert ok and ok[0].status == "ok" and not ok[0].dry


@pytest.mark.parametrize("kw,reason", [
    (dict(MAX_PRICE_TON=0.9), "MAX_PRICE_TON"),
    (dict(DAILY_BUDGET_TON=0.5), "daily budget"),
    (dict(MAX_BUYS_PER_HOUR=0), "MAX_BUYS_PER_HOUR"),
])
def test_limits_block_buys(kw, reason):
    reports, purchases, notifier, _ = asyncio.run(_pipeline(settings(**kw), force_dry=True, cycles=1))
    assert reports[0].skipped >= 1 and reports[0].dry_bought == 0
    assert purchases and all(p.status == "skipped" for p in purchases)
    assert any(reason in p.error for p in purchases)
    assert any(t.startswith("SKIPPED") for t in notifier.sent)


def test_bot_router_builds():
    from portals_autobuy.bot import AppContext, build_router

    async def go():
        s = settings(ADMIN_IDS="1, 2")
        storage = await Storage(":memory:").open()
        client = MockClient()
        engine = Engine(s, storage, client, Notifier(), force_dry=True)
        router = build_router(AppContext(s, storage, client, engine))
        await storage.close()
        return router

    router = asyncio.run(go())
    assert router.name == "admin"
    assert settings(ADMIN_IDS="1, 2").admin_ids == [1, 2]
