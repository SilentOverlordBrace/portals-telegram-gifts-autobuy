import json

import pytest

from portals_autobuy.models import Listing
from portals_autobuy.rules import (
    BuyRule,
    RuleParseError,
    discount_pct,
    load_rules_file,
    parse_addrule_args,
)


def L(price, collection="Lol Pop", model="Cookie Heart", backdrop="Black", symbol="Star", id="x"):
    return Listing(id=id, collection=collection, model=model, backdrop=backdrop, symbol=symbol,
                   number=1, price_ton=price, listed_at=None)


def test_discount_pct():
    assert discount_pct(2.0, 4.0) == pytest.approx(50.0)
    assert discount_pct(5.0, 4.0) == pytest.approx(-25.0)
    assert discount_pct(1.0, None) is None
    assert discount_pct(1.0, 0) is None


def test_match_ok_and_discount():
    rule = BuyRule(collection="Lol Pop", max_price_ton=3, min_discount_pct=30)
    res = rule.match(L(2.1), floor=4.0)
    assert res.ok and res.discount_pct == pytest.approx(47.5)


def test_match_rejects_over_max():
    rule = BuyRule(collection="Lol Pop", max_price_ton=3)
    res = rule.match(L(3.5), floor=10)
    assert not res.ok and "max" in res.reason


def test_match_rejects_small_discount():
    rule = BuyRule(collection="Lol Pop", max_price_ton=5, min_discount_pct=20)
    res = rule.match(L(3.9), floor=4.25)
    assert not res.ok and "discount" in res.reason


def test_match_requires_floor_when_discount_set():
    rule = BuyRule(collection="Lol Pop", max_price_ton=5, min_discount_pct=10)
    assert not rule.match(L(1), floor=None).ok
    # without a discount requirement the floor is irrelevant
    assert BuyRule(collection="Lol Pop", max_price_ton=5).match(L(1), floor=None).ok


def test_match_attributes_case_insensitive():
    rule = BuyRule(collection="lol pop", model="cookie  heart", backdrop="BLACK", max_price_ton=5)
    assert rule.match(L(1), floor=None).ok
    assert not rule.match(L(1, model="Rainbow"), floor=None).ok
    assert not rule.match(L(1, collection="Ice Cream"), floor=None).ok


def test_match_disabled_and_bad_price():
    assert not BuyRule(collection="Lol Pop", max_price_ton=5, enabled=False).match(L(1), None).ok
    assert not BuyRule(collection="Lol Pop", max_price_ton=5).match(L(0), None).ok


def test_parse_addrule_multiword():
    r = parse_addrule_args("Plush Pepe max=5000 model=Pepemint backdrop=Neon Blue symbol=Kitty Cat discount=15%")
    assert r.collection == "Plush Pepe"
    assert r.max_price_ton == 5000
    assert r.model == "Pepemint"
    assert r.backdrop == "Neon Blue"
    assert r.symbol == "Kitty Cat"
    assert r.min_discount_pct == 15


def test_parse_addrule_quotes_and_comma_decimal():
    r = parse_addrule_args('"Durov\'s Cap" max=4,5 backdrop="Neon Blue"')
    assert r.collection == "Durov's Cap"
    assert r.max_price_ton == 4.5
    assert r.backdrop == "Neon Blue"
    assert r.model is None and r.min_discount_pct == 0


@pytest.mark.parametrize("text", ["", "max=5", "Lol Pop", "Lol Pop max=abc", "Lol Pop max=0",
                                  "Lol Pop max=5 discount=100"])
def test_parse_addrule_errors(text):
    with pytest.raises(RuleParseError):
        parse_addrule_args(text)


def test_load_rules_file(tmp_path):
    p = tmp_path / "rules.json"
    assert load_rules_file(p) == []
    p.write_text(json.dumps([
        {"collection": "Lol Pop", "max_price_ton": 3, "min_discount_pct": 30},
        {"collection": "Ice Cream", "max": 4, "model": "Cocoa", "discount": 20},
    ]))
    rules = load_rules_file(p)
    assert [r.collection for r in rules] == ["Lol Pop", "Ice Cream"]
    assert rules[1].max_price_ton == 4 and rules[1].min_discount_pct == 20
    assert all(r.source == "file" for r in rules)
    p.write_text(json.dumps({"rules": [{"collection": "Lol Pop", "max": 1}]}))
    assert len(load_rules_file(p)) == 1
    p.write_text(json.dumps([{"collection": "Lol Pop"}]))
    with pytest.raises(RuleParseError):
        load_rules_file(p)
