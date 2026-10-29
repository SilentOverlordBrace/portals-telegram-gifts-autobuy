"""Safety parity with the sibling gift bots (mrkt-autobuy, tonnel-autobuy): every safety fix found in one of the
three reviews has a regression test here or in test_spend_guard.py / test_review_regressions.py. Covers arming
LIVE versus PRACTICE cards, the card's own practice flag, odd buy responses, an invalid live gate, pause and
breakers right before the buy, expiring buttons, shadow-first file rules, housekeeping after failed cycles and
the thin-floor confidence rule (two decoy listings never make a `med` floor)."""

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from helpers import ADMIN, Clock, L, demo_engine, settings

from portals_autobuy.client import DEMO_RULES, MockClient, PortalsError, classify_buy_response
from portals_autobuy.pricing import estimate_floor
from portals_autobuy.rules import STATE_UNSET, BuyRule, load_rules_file, match

CONFIRM_RULES = [replace(r, mode="confirm") if r.mode == "auto" else r for r in DEMO_RULES]
LIVE = dict(DRY_RUN=False, CONFIRM_ABOVE_TON=100, TG_VALUE_GUARD=False, BALANCE_GUARD=False)
CAKE = [BuyRule("Homemade Cake", 3.5, min_confidence="low", source="demo")]  # hc-1234 at 3.00, auto


# --- arming LIVE and the card's own practice flag ------------------------------------------------------------


def test_arming_live_expires_every_pending_practice_card_and_keeps_live_ones():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), rules=CONFIRM_RULES,
                                                                     force_dry=False)
        try:
            await engine.run_cycle()  # practice: every confirm card is a PRACTICE card
            practice = await storage.list_pending(("pending",))
            assert practice and all(c.payload["practice"] is True for c in practice)
            assert await engine.arm_live() is None
            assert await storage.list_pending(("pending",)) == []
            assert {(await storage.get_pending(c.token)).status for c in practice} == {"expired"}
            for card in practice:
                res = await engine.tap("b", card.token, ADMIN)
                assert not res.ok and res.text == "Expired or already handled"
            assert client.bought == []

            client.advance()
            clock.advance(5)
            await engine.run_cycle()  # LIVE now: new cards are LIVE cards, and renewing LIVE keeps them
            live = await storage.list_pending(("pending",))
            assert live and all(c.payload["practice"] is False for c in live)
            assert await engine.arm_live() is None
            assert {c.token for c in await storage.list_pending(("pending",))} == {c.token for c in live}
        finally:
            await storage.close()

    asyncio.run(go())


def test_a_practice_card_that_slips_through_only_ever_buys_in_practice():
    async def go():
        shadow = [replace(r, state="shadow") for r in CONFIRM_RULES]
        engine, storage, client, _, clock = await demo_engine(settings(DRY_RUN=False), rules=shadow, force_dry=False)
        try:
            assert await engine.arm_live() is None
            await engine.run_cycle()  # LIVE, but the rules are in shadow: the cards are PRACTICE cards
            cards = {c.listing_id: c for c in await storage.list_pending(("pending",))}
            assert cards["dc-3002"].payload["practice"] is True
            for rule in await storage.list_rules():
                await engine.promote(rule.id)  # promoted after the cards went out

            res = await engine.tap("b", cards["dc-3002"].token, ADMIN)
            assert res.ok and "PRACTICE" in res.text and client.bought == []

            # a card without the flag (written by an older build) counts as practice too
            card = cards["hc-1234"]
            payload = {k: v for k, v in card.payload.items() if k != "practice"}
            await storage.db.execute("UPDATE pending SET payload_json=? WHERE token=?", (json.dumps(payload), card.token))
            await storage.db.commit()
            res = await engine.tap("b", card.token, ADMIN)
            assert res.ok and "PRACTICE" in res.text and client.bought == []
            assert [p for p in await storage.recent_purchases() if not p.dry] == []
        finally:
            await storage.close()

    asyncio.run(go())


# --- only an explicit success is a buy -----------------------------------------------------------------------


@pytest.mark.parametrize("body", [{}, {"id": "ord-1"}, {"status": "success", "order_id": 7}, {"status": 200},
                                  {"success": True, "message": ""}])
def test_explicit_success_bodies_are_buys(body):
    ok, _ = classify_buy_response(body)
    assert ok


@pytest.mark.parametrize("body", [[{"id": "x"}], [], "OK", None, True, {"error": "sold"}, {"detail": "Not enough funds"},
                                  {"message": "Gift purchased"}, {"success": False}, {"ok": False},
                                  {"status": "pending"}, {"status": "error"}, {"status": 409}])
def test_any_other_buy_response_is_unknown_and_counts_toward_the_limits(body):
    class Odd(MockClient):
        async def buy(self, listing):
            await super().buy(listing)
            return copy.deepcopy(body)

    async def go():
        clock = Clock()
        engine, storage, client, notifier, clock = await demo_engine(
            settings(MAX_BUYS_PER_HOUR=1, AUTO_PAUSE_ERRORS=5, **LIVE), force_dry=False, clock=clock,
            client=Odd(now=clock()))
        try:
            assert await engine.arm_live() is None
            report = await engine.run_cycle()
            assert len(client.bought) == 1 and report.bought == 0 and report.failed == 1
            [p] = [p for p in await storage.recent_purchases() if not p.dry and p.status != "skipped"]
            assert p.status == "unknown" and "unrecognized buy response" in p.error
            assert await storage.buys_since(clock() - 3600, dry=False) == 1  # the hourly cap stopped the rest
            assert await storage.spent_since(clock() - 86400, dry=False) == pytest.approx(p.cost_ton)
            assert not any(m.startswith("BOUGHT") for m in notifier.messages)
        finally:
            await storage.close()

    asyncio.run(go())


# --- is_dry() fails closed -------------------------------------------------------------------------------------


@pytest.mark.parametrize("stored", ["inf", "nan", "-inf", "soon", "far"])
def test_an_invalid_live_gate_reads_as_practice_and_is_cleared(stored):
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(**LIVE), rules=CAKE, force_dry=False)
        try:
            assert await engine.is_dry()  # fresh database, DRY_RUN=false: practice until /dry off
            await storage.set_flag("dry", False)
            value = clock() + 10 * 24 * 3600 if stored == "far" else stored  # 10 days ahead of a 24 h gate
            await storage.set_setting("live_until", value)
            assert await engine.is_dry()
            await engine.run_cycle()
            assert client.bought == []
            if stored != "soon":  # unreadable text reads as no gate at all; the rest is cleared with a notice
                assert await engine.live_until() is None
                assert any("live gate was not valid" in m for m in notifier.messages)
            assert await engine.arm_live() is None and not await engine.is_dry()  # a real gate still works
        finally:
            await storage.close()

    asyncio.run(go())


# --- pause and breakers right before the buy request ----------------------------------------------------------


@pytest.mark.parametrize("trip", ["pause", "crash breaker", "losing streak", "rule paused"])
def test_pause_or_a_breaker_during_the_balance_call_stops_the_auto_buy(trip):
    class Hook(MockClient):
        hook = None

        async def balance(self):
            if self.hook:
                hook, self.hook = self.hook, None
                await hook()
            return 1000.0

    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(**LIVE), rules=CAKE, force_dry=False, clock=clock, client=Hook(now=clock()))
        try:
            assert await engine.arm_live() is None

            async def fire():
                if trip == "pause":
                    await engine.set_paused(True)
                elif trip == "crash breaker":
                    await storage.set_setting("rule_paused_until:1", clock() + 3600)
                elif trip == "losing streak":  # what _losing_streak does
                    await storage.set_flag("dry", True)
                    await storage.delete_setting("live_until")
                else:
                    await storage.update_rule(1, state="paused")

            client.hook = fire
            report = await engine.run_cycle()
            assert client.bought == [] and report.bought == 0
            assert [p for p in await storage.recent_purchases() if not p.dry] == []
        finally:
            await storage.close()

    asyncio.run(go())


# --- buttons expire -------------------------------------------------------------------------------------------


def test_buy_and_raise_max_buttons_expire_even_before_the_sweep():
    async def go():
        engine, storage, client, _, clock = await demo_engine(settings(DRY_RUN=False), rules=CONFIRM_RULES,
                                                              force_dry=False)
        try:
            assert await engine.arm_live() is None
            await engine.run_cycle()
            card = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == "dc-3002")
            near = next(c for c in await storage.list_pending(("near",)))
            clock.advance(91)  # past CONFIRM_TTL_SEC; tick() has not swept the card yet
            res = await engine.tap("b", card.token, ADMIN)
            assert not res.ok and res.text == "Expired or already handled" and client.bought == []
            rule = await storage.get_rule(near.rule_id)
            clock.advance(2 * 86400)
            res = await engine.near_action("r", near.token, ADMIN)
            assert not res.ok and res.text == "Expired or already handled"
            assert (await storage.get_rule(near.rule_id)).max_price_ton == rule.max_price_ton
        finally:
            await storage.close()

    asyncio.run(go())


# --- rules.json rules start in shadow -------------------------------------------------------------------------


def test_new_file_rules_start_in_shadow_and_a_restart_keeps_their_state(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"collection": "Lunar Snake", "max": 2},
                                {"collection": "Desk Calendar", "max": 3, "state": "live"}]))
    assert [r.state for r in load_rules_file(path)] == [STATE_UNSET, "live"]

    async def go():
        engine, storage, *_ = await demo_engine(settings(RULES_FILE=str(path)), rules=[])
        try:
            await engine.sync_rules_file()
            snake, calendar = await storage.list_rules()
            assert (snake.state, calendar.state) == ("shadow", "live")  # only an explicit state skips shadow
            await engine.promote(snake.id)
            await storage.update_rule(calendar.id, state="paused")
            await engine.sync_rules_file()  # restart: the promotion stays, the explicit file state wins
            snake, calendar = await storage.list_rules()
            assert (snake.state, calendar.state) == ("live", "live")
        finally:
            await storage.close()
        engine, storage, *_ = await demo_engine(settings(RULES_FILE=str(path), SHADOW_NEW_RULES=False), rules=[])
        try:
            await engine.sync_rules_file()
            assert [r.state for r in await storage.list_rules()] == ["live", "live"]
        finally:
            await storage.close()

    asyncio.run(go())


# --- housekeeping keeps running while cycles fail ---------------------------------------------------------------


def test_cards_expire_and_the_live_gate_ends_while_cycles_fail():
    class Down(MockClient):
        down = False

        async def search_listings(self, filters):
            if self.down:
                raise PortalsError("GET /nfts/search: HTTP 503: down", 503)
            return await super().search_listings(filters)

    async def go():
        clock = Clock()
        engine, storage, client, notifier, clock = await demo_engine(
            settings(DRY_RUN=False, LIVE_ARM_HOURS=1), rules=CONFIRM_RULES, force_dry=False, clock=clock,
            client=Down(now=clock()))
        try:
            assert await engine.arm_live() is None
            await engine.run_cycle()
            assert await storage.list_pending(("pending",))
            client.down = True
            clock.advance(2 * 3600)
            before = client.requests
            await engine.run_forever(asyncio.Event(), max_cycles=engine.stats.cycles + 1)
            assert engine.stats.consecutive_errors == 1
            assert await storage.list_pending(("pending",)) == []  # expired by tick() after the failed cycle
            assert any("Live mode ended after 1 h" in m for m in notifier.messages)
            assert client.requests - before <= 2  # the failed search only: no mark searches on a struggling API
        finally:
            await storage.close()

    asyncio.run(go())


# --- thin floors: two decoy listings never make a `med` floor ----------------------------------------------------


def test_a_floor_without_a_cluster_of_three_is_low_confidence():
    candidate = L("c", 1.0)
    decoys = [L("d1", 2.0), L("d2", 2.05)]
    for k in (1, 2):  # CLUSTER_K=1 turns two decoys into a "cluster" of two: still low
        est = estimate_floor(candidate, decoys, 2.0, [], 0.0, k=k, min_comparables=2)
        assert est.value == pytest.approx(2.0 if k == 1 else 2.025) and est.confidence == "low"
        result = match(BuyRule("Lunar Snake", 3.0), candidate, assess_deal(candidate, est))
        assert not result.ok and result.code == "LOW_CONFIDENCE"
    three = estimate_floor(candidate, [*decoys, L("d3", 2.1)], None, [], 0.0, min_comparables=5)
    assert (three.source, three.confidence) == ("cluster", "med")


def test_the_risk_line_says_why_a_floor_is_thin():
    from portals_autobuy.notifier import DealView, risk_line

    candidate = L("c", 1.0)

    def risk(est):
        view = DealView(listing=candidate, deal=assess_deal(candidate, est), rule=BuyRule("Lunar Snake", 3.0),
                        mode="confirm", status="", practice=True)
        return risk_line(view)

    four = [L("a", 1.6), L("b", 1.62), L("c2", 3.0), L("d", 4.0)]  # no three asks within 10%
    assert risk(estimate_floor(candidate, four, None, [], 0.0)) == (
        "Risk: thin floor, 4 comparable listings but no cluster of 3 similar asks.")
    assert risk(estimate_floor(candidate, [L("a", 1.6)], None, [], 0.0)) == (
        "Risk: thin floor, only 1 comparable listing.")
    jumpy = estimate_floor(candidate, [L("a", 1.6), L("b", 1.62), L("d", 1.65)], None,
                           [(-1800.0, 2.4), (-60.0, 1.6)], 0.0)
    assert jumpy.confidence == "low" and risk(jumpy).startswith("Risk: the floor moved fast")


def assess_deal(listing, est):
    from portals_autobuy.pricing import FeeModel, assess

    return assess(listing, est, FeeModel(0, 5, 0.1), [])


@pytest.mark.parametrize("k", [1, 2])
def test_two_decoy_listings_cannot_get_a_default_rule_to_buy(k):
    async def go():
        clock = Clock()
        client = MockClient(now=clock())
        decoy = copy.deepcopy(next(i for i in client._items if i["id"] == "hc-1240"))
        decoy.update(id="hc-1241", external_collection_number=1241, price="4.1")
        client._items.append(decoy)  # hc-1234 at 3.00 against two decoys at 4.00 and 4.10
        engine, storage, client, _, clock = await demo_engine(
            settings(CLUSTER_K=k, **LIVE), rules=[BuyRule("Homemade Cake", 3.5, source="demo")], force_dry=False,
            clock=clock, client=client)
        try:
            assert await engine.arm_live() is None
            report = await engine.run_cycle()
            assert client.bought == [] and report.reasons["LOW_CONFIDENCE"] >= 1
            last = (await storage.decisions_for("hc-1234"))[-1]
            assert last.reason_code == "LOW_CONFIDENCE"
        finally:
            await storage.close()

    asyncio.run(go())
