import asyncio
import sqlite3

import pytest
from helpers import NOW, L, demo_engine

from portals_autobuy.client import DEMO_COLLECTIONS, demo_listings
from portals_autobuy.models import CollectionInfo, Listing
from portals_autobuy.pricing import (
    FeeModel,
    FloorEstimate,
    assess,
    classify,
    cluster_floor,
    estimate_floor,
    too_good_flag,
)
from portals_autobuy.rules import BuyRule, match, parse_addrule_args
from portals_autobuy.storage import Storage


def pool(*prices, model="Albino"):
    return [L(f"p{i}", p, model=model) for i, p in enumerate(prices)]


# --- robust floor -----------------------------------------------------------------------


def test_cluster_floor_ignores_one_troll_ask():
    # F1: 1.00 (the candidate), 3.00 troll, cluster at 1.60/1.62/1.65
    candidate = L("c", 1.00)
    est = estimate_floor(candidate, [candidate, *pool(3.00, 1.60, 1.62, 1.65)], None, [], NOW, min_comparables=3)
    assert est.value == pytest.approx(1.60)
    assert est.source == "cluster" and est.n == 4 and est.confidence == "high"


def test_lone_cheap_ask_is_not_the_floor():
    candidate = L("c", 1.40)
    est = estimate_floor(candidate, pool(0.50, 2.00, 2.05, 2.10), None, [], NOW)
    assert est.value == pytest.approx(2.00)
    assert cluster_floor([0.5, 2.0, 2.05, 2.1]) == 2.0


def test_thin_book_gives_low_confidence():
    candidate = L("c", 1.0)
    est = estimate_floor(candidate, pool(1.5), 1.4, [], NOW)
    assert est.source == "thin" and est.value == 1.5 and est.confidence == "low"
    assert estimate_floor(candidate, [], 1.4, [], NOW).source == "api"
    assert estimate_floor(candidate, [], None, [], NOW).value is None
    two = estimate_floor(candidate, pool(1.5, 3.0), None, [], NOW)
    assert two.source == "thin" and two.value == pytest.approx(2.25) and two.confidence == "low"
    assert estimate_floor(candidate, [], 1.4, [], NOW).confidence == "low"


def test_snapshot_jump_downgrades_confidence():
    candidate = L("c", 1.0)
    book = pool(1.60, 1.62, 1.65, 1.66, 1.70)
    calm = estimate_floor(candidate, book, None, [(NOW - 1800, 1.58), (NOW, 1.60)], NOW)
    assert calm.confidence == "high" and calm.note == ""
    jumpy = estimate_floor(candidate, book, None, [(NOW - 1800, 2.40), (NOW - 60, 1.60)], NOW)
    assert jumpy.confidence == "med"
    assert jumpy.note == "floor -33% in 1h"
    old = estimate_floor(candidate, book, None, [(NOW - 7200, 2.40), (NOW, 1.60)], NOW)
    assert old.confidence == "high"


# --- fees ------------------------------------------------------------------------------------


def test_fee_model_tonnel_buy_fee():
    tonnel = FeeModel(buy_pct=10, sell_pct=0, withdraw_ton=0)
    assert tonnel.cost(0.97) == pytest.approx(1.067)
    assert tonnel.net_exit(1.0) == pytest.approx(1.0)


def test_fee_model_portals_withdraw():
    portals = FeeModel(buy_pct=0, sell_pct=5, withdraw_ton=0.1)
    assert portals.cost(2.0) == pytest.approx(2.0)
    assert portals.net_exit(2.0) == pytest.approx(1.8)


def test_mrkt_sell_fee_example_from_sale_price_without_fee():
    # MRKT derives its sell fee from salePrice vs salePriceWithoutFee; the same FeeModel covers it.
    mrkt = FeeModel(buy_pct=0, sell_pct=round((1 - 1.9 / 2.0) * 100, 2), withdraw_ton=0)
    assert mrkt.sell_pct == pytest.approx(5.0) and mrkt.net_exit(2.0) == pytest.approx(1.9)


def test_portals_zero_commission_collection_has_no_sell_fee():
    info = CollectionInfo.from_api({"id": "u", "name": "Jelly Bunny", "short_name": "jellybunny", "floor_price": "1.1",
                                    "zero_commission": True, "listed_count": 410, "is_premarket": False})
    assert info.zero_commission and info.listed_count == 410 and not info.is_premarket
    assert not CollectionInfo.from_api({"id": "v", "name": "X", "floor_price": "1"}).zero_commission

    async def go():
        engine, storage, client, *_ = await demo_engine()
        try:
            requests = client.requests
            await engine.run_cycle()
            assert engine.fees_for("Jelly Bunny").sell_pct == 0
            assert engine.fees_for("Jelly Bunny").withdraw_ton == pytest.approx(0.1)  # only the sell fee is waived
            assert engine.fees_for("Lunar Snake").sell_pct == engine.settings.SELL_FEE_PCT == 5
            assert client.requests - requests == 9  # collection_meta rides on the same cached /collections call
        finally:
            await storage.close()

    asyncio.run(go())


@pytest.mark.parametrize("fees", [FeeModel(10, 0, 0), FeeModel(0, 5, 0.1), FeeModel(0, 5, 0), FeeModel(3, 8, 0.05)])
def test_break_even_formula(fees):
    floor = 2.0
    be = fees.break_even(floor)
    assert be == pytest.approx((floor * (1 - fees.sell_pct / 100) - fees.withdraw_ton) / (1 + fees.buy_pct / 100))
    assert fees.cost(be) == pytest.approx(fees.net_exit(floor))  # paying the break-even price nets zero


def test_assess_deal_math():
    deal = assess(L("x", 1.0), FloorEstimate(1.6, "cluster", 4, "high"), FeeModel(0, 5, 0), [])
    assert deal.cost == 1.0 and deal.net_exit == pytest.approx(1.52)
    assert deal.profit_ton == pytest.approx(0.52) and deal.profit_pct == pytest.approx(52)
    assert deal.break_even_price == pytest.approx(1.52) and deal.discount_pct == pytest.approx(37.5)


# --- traps and locks ----------------------------------------------------------------------------


def test_classify_every_fixture_raw():
    initial, later = demo_listings(NOW)
    flags = {raw["id"]: [f.code for f in classify(Listing.from_api(raw), "portals", NOW)] for raw in initial + later}
    assert flags["jb-53"] == ["LOCKED"]
    assert flags["jb-54"] == ["TRAP_BUNDLE"]
    locked = classify(Listing.from_api(next(r for r in initial if r["id"] == "jb-53")), "portals", NOW)[0]
    assert locked.days == 6 and not locked.blocking and "6 d" in locked.text
    bundle = classify(Listing.from_api(next(r for r in initial if r["id"] == "jb-54")), "portals", NOW)[0]
    assert bundle.blocking
    assert all(codes == [] for lid, codes in flags.items() if lid not in ("jb-53", "jb-54"))
    assert {c["name"] for c in DEMO_COLLECTIONS} >= {raw["name"] for raw in initial + later}


def test_classify_fails_closed():
    assert [f.code for f in classify(L("z", 0), "portals", NOW)] == ["BAD_PRICE"]
    weird = classify(L("u", 1.0, currency="STARS"), "portals", NOW)
    assert weird[0].code == "BAD_PRICE" and weird[0].blocking
    # a missing unit is blocking too: no explicit TON currency and no `price` field (TON) to have read it from
    missing = classify(Listing("m", "Lunar Snake", None, None, None, 1, 1.0, raw={}), "portals", NOW)
    assert [(f.code, f.blocking) for f in missing] == [("BAD_PRICE", True)] and "unit unknown" in missing[0].text
    assert classify(Listing("t", "Lunar Snake", None, None, None, 1, 1.0, raw={"currency": "TON"}), "portals", NOW) == []
    # unlocks_at in the past is not a lock; lucky_buy_eligible is a badge only (seen on an ordinary listing)
    assert classify(L("p", 1.0, unlocks_at="2020-01-01T00:00:00Z"), "portals", NOW) == []
    assert classify(L("q", 1.0, lucky_buy_eligible=True, bundle_id=None), "portals", NOW) == []
    nanos = classify(L("n", 1.0, unlocks_at="2027-01-15T10:00:00.123456789Z"), "portals", NOW)
    assert nanos[0].code == "LOCKED" and nanos[0].days > 0


def test_too_good_flag():
    assert too_good_flag(0.16, 1.6).text == "price is 10% of floor"
    assert too_good_flag(1.0, 1.6) is None and too_good_flag(1.0, None) is None


# --- match() order and reason codes -------------------------------------------------------------


def deal_for(listing, floor=1.6, conf="high", n=5, fees=FeeModel(0, 5, 0), flags=()):
    flags = list(flags) or classify(listing, "portals", NOW)
    tg = too_good_flag(listing.price_ton, floor)
    if tg:
        flags.append(tg)
    return assess(listing, FloorEstimate(floor, "cluster", n, conf), fees, flags)


@pytest.mark.parametrize(
    "rule,listing,kw,code",
    [
        (BuyRule("Lunar Snake", 5, model="Cobra"), L("a", 1.0), {}, "MISMATCH"),
        (BuyRule("Lunar Snake", 0.5), L("b", 1.0, bundle_id="b1"), {}, "TRAP_BUNDLE"),  # trap before max
        (BuyRule("Lunar Snake", 0.5), L("c", 1.0, unlocks_at="2027-01-15T10:00:00Z"), {}, "LOCKED"),  # lock before max
        (BuyRule("Lunar Snake", 0.9), L("d", 1.0), {"conf": "low"}, "OVER_MAX"),  # max before confidence
        (BuyRule("Lunar Snake", 5, min_discount_pct=50), L("e", 1.0), {"conf": "low"}, "LOW_CONFIDENCE"),
        (BuyRule("Lunar Snake", 5, min_discount_pct=50), L("f", 1.0), {}, "LOW_DISCOUNT"),
        (BuyRule("Lunar Snake", 5), L("g", 0.97), {"floor": 1.0}, "LOSES_AFTER_FEES"),
        (BuyRule("Lunar Snake", 5), L("g2", 0.90), {"floor": 1.0, "fees": FeeModel(0, 5, 0.1)}, "LOSES_AFTER_FEES"),
        (BuyRule("Lunar Snake", 5, min_profit_ton=1), L("h", 1.0), {}, "LOW_PROFIT"),
        (BuyRule("Lunar Snake", 5, min_profit_pct=80), L("i", 1.0), {}, "LOW_PROFIT"),
    ],
)
def test_match_reason_codes_and_order(rule, listing, kw, code):
    res = match(rule, listing, deal_for(listing, **kw))
    assert not res.ok and res.code == code


def test_match_on_tonnel_like_fees_rejects_f2_with_loses_after_fees():
    listing = L("f2", 0.97)
    deal = deal_for(listing, floor=1.0, fees=FeeModel(10, 0, 0))
    res = match(BuyRule("Lunar Snake", 5), listing, deal)
    assert res.code == "LOSES_AFTER_FEES" and "0.07" in res.reason


def test_max_price_compares_cost_incl_buy_fee():
    listing = L("m", 0.95)
    deal = deal_for(listing, floor=2.0, fees=FeeModel(10, 0, 0))
    res = match(BuyRule("Lunar Snake", 1.0), listing, deal)
    assert res.code == "OVER_MAX" and "cost 1.04" in res.reason


def test_locked_allowed_by_rule_and_too_good_passes_match():
    listing = L("lk", 1.0, unlocks_at=NOW + 3 * 86400 - 60)
    assert match(BuyRule("Lunar Snake", 5, allow_locked_days=5), listing, deal_for(listing)).ok
    assert match(BuyRule("Lunar Snake", 5, allow_locked_days=2), listing, deal_for(listing)).code == "LOCKED"
    cheap = L("tg", 0.16)
    assert match(BuyRule("Lunar Snake", 5), cheap, deal_for(cheap)).ok  # TOO_GOOD is not blocking


# --- /addrule ------------------------------------------------------------------------------------


def test_addrule_parses_new_keys():
    rule = parse_addrule_args("Lunar Snake max=3 profit=0.3 profit%=10 conf=high locked=7 mode=confirm exit=best")
    assert (rule.min_profit_ton, rule.min_profit_pct, rule.min_confidence, rule.allow_locked_days) == (0.3, 10, "high", 7)
    assert (rule.mode, rule.exit) == ("confirm", "best")
    default = parse_addrule_args("Lunar Snake max=3")
    assert (default.min_confidence, default.mode, default.exit, default.allow_locked_days) == ("med", "auto", "here", 0)


@pytest.mark.parametrize("text", ["X max=1 conf=great", "X max=1 mode=yolo", "X max=1 profit=-1", "X max=1 locked=abc"])
def test_addrule_rejects_bad_new_keys(text):
    from portals_autobuy.rules import RuleParseError

    with pytest.raises(RuleParseError):
        parse_addrule_args(text)


# --- migration ------------------------------------------------------------------------------------

# The portals-autobuy scaffold schema (commit "Initial scaffold").
SCAFFOLD_SCHEMA = """
CREATE TABLE seen (nft_id TEXT NOT NULL, price TEXT NOT NULL, first_seen REAL NOT NULL, PRIMARY KEY (nft_id, price));
CREATE TABLE purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, nft_id TEXT NOT NULL, collection TEXT,
    model TEXT, backdrop TEXT, symbol TEXT, number INTEGER, price_ton REAL NOT NULL, floor_ton REAL, rule_id INTEGER,
    dry INTEGER NOT NULL, status TEXT NOT NULL, error TEXT);
CREATE INDEX purchases_ts ON purchases (ts);
CREATE TABLE rules (id INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT NOT NULL, model TEXT, backdrop TEXT,
    symbol TEXT, max_price_ton REAL NOT NULL, min_discount_pct REAL NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'bot', created_at REAL NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_migration_from_scaffold_schema(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(SCAFFOLD_SCHEMA)
    con.execute("INSERT INTO rules(collection, max_price_ton, min_discount_pct, created_at) VALUES ('Lunar Snake', 2.5, 20, 1)")
    con.execute(
        "INSERT INTO purchases(ts, nft_id, collection, model, backdrop, number, price_ton, dry, status) VALUES "
        "(1, 'g1', 'Lunar Snake', 'Albino', 'Onyx', 7, 1.2, 0, 'ok')"
    )
    con.execute("INSERT INTO settings(key, value) VALUES ('dry', '0')")
    con.commit()
    con.close()

    async def go():
        storage = await Storage(str(path), buy_fee_pct=10).open()
        try:
            rules = await storage.list_rules()
            assert len(rules) == 1 and rules[0].max_price_ton == 2.5 and rules[0].min_discount_pct == 20
            assert (rules[0].mode, rules[0].min_confidence, rules[0].state) == ("auto", "med", "live")
            positions = await storage.positions()
            assert positions[0].cost_ton == pytest.approx(1.32) and positions[0].model == "Albino"
            assert positions[0].title == "Lunar Snake #7"
            [old] = await storage.recent_purchases()
            assert (old.nft_id, old.status, old.cost_ton) == ("g1", "ok", pytest.approx(1.32))
            for table in ("floor_snapshots", "observations", "decisions", "pending", "mutes", "marks"):
                async with storage.db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                    assert (await cur.fetchone())[0] == 0
            cols = await storage._columns("rules")
            assert {"min_profit_ton", "min_profit_pct", "min_confidence", "allow_locked_days", "mode", "exit"} <= cols
            assert {"cost_ton", "closed_at", "exit_price_ton", "exit_note"} <= await storage._columns("purchases")
        finally:
            await storage.close()
        again = await Storage(str(path)).open()  # second open is a no-op migration
        try:
            assert len(await again.list_rules()) == 1
        finally:
            await again.close()

    asyncio.run(go())
