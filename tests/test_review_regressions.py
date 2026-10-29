"""Regression tests for the review findings: a live buy cut off by shutdown still counts, a PRACTICE card never
buys live, a 0 max in rules.json, the buy path never waits on the TON rate, LIVE fails closed, a rule edited
while its buy is in flight, best exit with a custom bot username, /buys tags, the digest budget line, and a
fix hint on every /doctor warning."""

import asyncio
import json
from dataclasses import replace

import pytest
from helpers import ADMIN, NOW, Clock, L, demo_engine, settings

from portals_autobuy import doctor
from portals_autobuy.bot import purchase_tag
from portals_autobuy.client import DEMO_RULES, MockClient
from portals_autobuy.digest import build_digest
from portals_autobuy.fx import TonRate
from portals_autobuy.notifier import DealView, render_deal_card
from portals_autobuy.peers import FakeTgValue, Peers, TgValueService
from portals_autobuy.pricing import FeeModel, FloorEstimate, assess
from portals_autobuy.rules import BuyRule, RuleParseError, load_rules_file, near_miss

CONFIRM_RULES = [replace(r, mode="confirm") if r.mode == "auto" else r for r in DEMO_RULES]
CAKE = [BuyRule("Homemade Cake", 3.5, min_confidence="low", source="demo")]  # hc-1234 at 3.00, auto
RATE_JSON = {"rates": {"TON": {"prices": {"USD": 1.25}}}}


# --- medium: a live buy cancelled mid-request still counts toward the limits ------------------------------


class CancelOnBuy(MockClient):
    async def buy(self, listing):
        self.bought.append(listing.id)  # the order reached Portals...
        raise asyncio.CancelledError()  # ...then shutdown cancelled the task before the answer came back


def test_live_buy_cancelled_mid_request_still_counts_toward_the_limits():
    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False, CONFIRM_ABOVE_TON=100, TG_VALUE_GUARD=False), rules=CAKE, force_dry=False,
            clock=clock, client=CancelOnBuy(now=clock()))
        try:
            assert await engine.arm_live() is None
            with pytest.raises(asyncio.CancelledError):
                await engine.run_cycle()
            assert client.bought == ["hc-1234"]
            [p] = [p for p in await storage.recent_purchases() if not p.dry]
            assert p.status == "unknown" and "interrupted" in p.error
            assert await storage.buys_since(clock() - 3600, dry=False) == 1
            assert await storage.spent_since(clock() - 86400, dry=False) == pytest.approx(p.cost_ton)
        finally:
            await storage.close()

    asyncio.run(go())


def test_drain_lets_the_buy_in_flight_settle_and_refuses_new_ones():
    class SlowBuy(MockClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.in_flight = False

        async def buy(self, listing):
            self.in_flight = True
            await asyncio.sleep(0.05)
            return await super().buy(listing)

    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False), rules=CONFIRM_RULES, force_dry=False, clock=clock,
            client=SlowBuy(now=clock()))
        try:
            assert await engine.arm_live() is None
            await engine.run_cycle()
            cards = {c.listing_id: c.token for c in await storage.list_pending(("pending",))}
            first = asyncio.create_task(engine.tap("b", cards["dc-3002"], ADMIN))
            for _ in range(500):  # wait until the tap is inside buy(), whatever the event loop
                if client.in_flight:
                    break
                await asyncio.sleep(0.01)
            assert await engine.drain(5) is True
            [p] = [p for p in await storage.recent_purchases() if not p.dry]
            assert p.status == "ok" and p.nft_id == "dc-3002"  # settled before drain() returned
            assert (await first).ok
            late = await engine.tap("b", cards["hc-1234"], ADMIN)
            assert not late.ok and "shutting down" in late.text
            assert client.bought == ["dc-3002"]
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: a card labelled PRACTICE never spends real TON -------------------------------------------------


def test_practice_card_tapped_after_live_is_armed_does_not_buy_live():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), rules=CONFIRM_RULES,
                                                                     force_dry=False)
        try:
            await engine.start_in_practice()
            await engine.run_cycle()
            card = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == "hc-1234")
            assert card.payload["practice"] is True
            assert await engine.arm_live() is None  # arming LIVE expires every pending PRACTICE card at once
            assert await storage.list_pending(("pending",)) == []
            clock.advance(10)
            res = await engine.tap("b", card.token, ADMIN)
            assert not res.ok and res.text == "Expired or already handled"
            assert client.bought == []
            assert (await storage.get_pending(card.token)).status == "expired"
            assert any("LIVE was armed after this PRACTICE card" in e["note"] for e in notifier.edits)
            assert not [p for p in await storage.recent_purchases() if not p.dry]
        finally:
            await storage.close()

    asyncio.run(go())


def test_practice_card_tapped_in_practice_still_does_a_practice_buy():
    async def go():
        engine, storage, client, _, clock = await demo_engine(rules=CONFIRM_RULES)
        try:
            await engine.run_cycle()
            card = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == "dc-3002")
            res = await engine.tap("b", card.token, ADMIN)
            assert res.ok and "PRACTICE" in res.text and client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: rules.json with max_price_ton 0 ------------------------------------------------------------------


def test_rules_file_rejects_a_zero_or_negative_max(tmp_path):
    for bad in (0, -1, "nan"):
        with pytest.raises(RuleParseError, match="max_price_ton must be > 0"):
            BuyRule.from_dict({"collection": "Lunar Snake", "max_price_ton": bad, "model": "Albino"})
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"collection": "Lunar Snake", "max_price_ton": 0}]))
    with pytest.raises(RuleParseError):
        load_rules_file(path)


def test_a_stored_zero_max_rule_does_not_crash_the_cycle():
    async def go():
        rule = BuyRule("Lunar Snake", 0, model="Albino", source="demo")  # e.g. a row from an older version
        engine, storage, client, _, clock = await demo_engine(rules=[rule])
        try:
            report = await engine.run_cycle()
            assert report.matched == 0 and report.reasons["OVER_MAX"] >= 1
            deal = assess(L("x", 1.0), FloorEstimate(1.6, "cluster", 4, "high"), FeeModel(0, 5, 0), [])
            assert near_miss(rule, L("x", 1.0), deal) is None
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: the buy path never waits on tonapi ---------------------------------------------------------------


def test_cycle_without_a_telegram_session_never_waits_on_the_ton_rate():
    async def go():
        gate = asyncio.Event()
        fetches = []

        async def blocked_fetch(url):
            fetches.append(url)
            await gate.wait()  # tonapi slow or unreachable
            return RATE_JSON

        s = settings(PEERS_TIMEOUT_SEC=5)
        clock = Clock()
        tg = TgValueService(None, clock=clock)  # what build_peers() wires up without a session
        peers = Peers(s, "portals", tg=tg, rate=TonRate(s.TON_RATE_URL, fetch=blocked_fetch, clock=clock), clock=clock)
        rules = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="demo")]
        engine, storage, client, _, clock = await demo_engine(s, rules=rules, peers=peers, clock=clock)
        try:
            report = await asyncio.wait_for(engine.run_cycle(), 1.0)
            assert report.dry_bought == 1
            assert len(fetches) == 1  # only the engine's background refresh
            gate.set()
            await engine._rate_task
            assert peers.rate.value == 1.25
        finally:
            gate.set()
            await storage.close()

    asyncio.run(go())


def test_peer_view_uses_the_cached_rate_while_a_refresh_is_slow():
    async def go():
        gate = asyncio.Event()
        fetches = []

        async def fetch(url):
            fetches.append(url)
            if len(fetches) > 1:
                await gate.wait()
            return RATE_JSON

        clock = Clock()
        rate = TonRate("x", ttl=600, fetch=fetch, clock=clock)
        assert await rate.usd_per_ton() == 1.25
        clock.advance(601)  # stale now: the next refresh hangs
        tg = TgValueService(FakeTgValue({"LunarSnake-1": {"currency": "USD", "average_price": 300}}), clock=clock)
        peers = Peers(settings(PEERS_TIMEOUT_SEC=5), "portals", tg=tg, rate=rate, clock=clock)
        refresh = asyncio.create_task(rate.usd_per_ton())  # the engine's background refresh
        await asyncio.sleep(0)
        view = await asyncio.wait_for(peers.view(L("x", 1.0, tg_id="LunarSnake-1"), BuyRule("Lunar Snake", 5), None),
                                      0.5)
        assert view.tg is not None and view.tg.usd_per_ton == 1.25 and view.tg.guard_ton == pytest.approx(2.4)
        again = asyncio.create_task(rate.usd_per_ton())  # a second caller joins the request in flight
        await asyncio.sleep(0)
        gate.set()
        assert await refresh == 1.25 and await again == 1.25
        assert len(fetches) == 2

    asyncio.run(go())


# --- low: LIVE fails closed -------------------------------------------------------------------------------------


def test_live_needs_an_armed_gate_even_with_dry_run_false():
    async def go():
        engine, storage, client, _, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            assert await engine.is_dry() and await engine.live_until() is None
            await storage.set_flag("dry", False)  # a stored flag alone (no expiring gate) is not enough
            assert await engine.is_dry()
            await engine.run_cycle()
            assert client.bought == []
            assert await engine.arm_live() is None and not await engine.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: the rule as stored now decides the auto buy ----------------------------------------------------------


class HookClient(MockClient):
    hook = None

    async def balance(self):
        if self.hook:
            hook, self.hook = self.hook, None
            await hook()
        return 1000.0


@pytest.mark.parametrize("change, code", [({"mode": "watch"}, "MODE_CHANGED"), ({"max_price_ton": 1.0}, "OVER_MAX")])
def test_rule_edited_during_the_balance_call_stops_the_auto_buy(change, code):
    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False, CONFIRM_ABOVE_TON=100, BALANCE_GUARD=False, TG_VALUE_GUARD=False),
            rules=CAKE, force_dry=False, clock=clock, client=HookClient(now=clock()))
        try:
            assert await engine.arm_live() is None

            async def user_edits_rule():
                await storage.update_rule(1, **change)

            client.hook = user_edits_rule
            report = await engine.run_cycle()
            assert client.bought == [] and report.skipped == 1
            last = (await storage.decisions_for("hc-1234"))[-1]
            assert (last.outcome, last.reason_code) == ("skip", code)
            assert not [p for p in await storage.recent_purchases() if not p.dry]
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: card, /buys, digest, doctor ----------------------------------------------------------------------------


def _card_view(best):
    listing = L("ls-4410", 1.0, number=4410)
    deal = assess(listing, FloorEstimate(1.6, "cluster", 4, "high"), FeeModel(10, 5, 0.1), [])
    return DealView(listing=listing, deal=deal, rule=BuyRule("Lunar Snake", 3, id=1), mode="confirm", status="",
                    practice=True, token="AbCdEf12", now=NOW, best_exit=best, market="portals",
                    market_bot="MyPortalsMirrorBot")


def test_best_exit_line_is_hidden_for_our_own_market_with_a_custom_bot_username():
    assert "Best exit" not in render_deal_card(_card_view(("portals", 1.5))).plain
    assert "Best exit: MRKT ~1.80" in render_deal_card(_card_view(("mrkt", 1.8))).plain


def test_buys_tags_say_practice_or_live_and_what_happened():
    assert purchase_tag(True, "dry") == "PRACTICE bought"
    assert purchase_tag(True, "skipped") == "PRACTICE not bought"
    assert purchase_tag(False, "ok") == "LIVE bought"
    assert purchase_tag(False, "unknown") == "LIVE result unknown"
    assert purchase_tag(False, "error") == "LIVE failed"
    assert purchase_tag(False, "skipped") == "LIVE not bought"


def test_digest_shows_spend_against_the_budget():
    async def go():
        engine, storage, client, _, clock = await demo_engine()
        try:
            await engine.run_cycle()
            practice = await storage.spent_since(clock() - 86400, dry=True)
            assert practice > 0
            text = await build_digest(engine, clock() - 86400, clock() + 1)
            assert f"Spent vs budget: practice {practice:.2f} · live 0.00 of DAILY_BUDGET_TON 50 TON" in text
        finally:
            await storage.close()

    asyncio.run(go())


def test_every_doctor_warning_or_failure_names_a_fix(tmp_path):
    async def go():
        s = settings(DATA_DIR=str(tmp_path))
        bare = doctor.DoctorDeps(s, None, session_file=tmp_path / "missing.session", now=NOW)
        checks = [await fn(bare) for _, fn in doctor.CHECKS if fn is not doctor.check_clock]
        bad = [c for c in checks if c.status != "ok"]
        assert bad and all(c.fix for c in bad), [c.line() for c in bad if not c.fix]
        session = next(c for c in checks if c.name == "Session file permissions")
        assert "portals_autobuy login" in session.fix

    asyncio.run(go())
