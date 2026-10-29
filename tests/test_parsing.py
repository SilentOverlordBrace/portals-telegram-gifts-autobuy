from urllib.parse import quote, urlencode, quote_plus

from portals_autobuy.client import SORT_LATEST, SORT_PRICE_ASC, SearchFilters, build_search_params
from portals_autobuy.models import AttributeFloors, CollectionInfo, Listing, parse_ts, short_name
from portals_autobuy.tg_auth import extract_init_data, mask

# Shape copied from a live GET /nfts/search response (2026-10-28), trimmed.
SEARCH_ITEM = {
    "id": "019ead3d-9c38-7015-b6e3-822d3ef8255d",
    "bundle_id": None,
    "tg_id": "PlushPepe-2232",
    "collection_id": "8d21b568-63bb-437a-9093-8684c5b63485",
    "external_collection_number": 2232,
    "name": "Plush Pepe",
    "price": "6598.99",
    "attributes": [
        {"type": "model", "value": "Pepemint", "rarity_per_mille": 3},
        {"type": "symbol", "value": "Kitty Cat", "rarity_per_mille": 0.3},
        {"type": "backdrop", "value": "Neon Blue", "rarity_per_mille": 2},
    ],
    "listed_at": "2026-10-28T18:47:34.906382Z",
    "status": "listed",
    "floor_price": "6400",
}

# Shape from live GET /collections/filters?short_names=icecream (trimmed).
FILTERS = {
    "models": [
        {"name": "Cryo Pepe", "rarity_per_mille": 0.1, "collection": "icecream", "floor_price": "86", "supply": 27},
        {"name": "Poopsicle", "rarity_per_mille": 0.2, "collection": "icecream", "floor_price": "6.8", "supply": 20},
    ],
    "symbols": [{"name": "Astronaut", "rarity_per_mille": 0.2, "floor_price": "4.5", "supply": 70}],
    "backdrops": [{"name": "Burgundy", "rarityPermille": 1, "floor_price": "4", "supply": 350}],
}


def test_listing_from_api():
    lst = Listing.from_api(SEARCH_ITEM)
    assert lst.id == SEARCH_ITEM["id"]
    assert lst.collection == "Plush Pepe"
    assert (lst.model, lst.backdrop, lst.symbol) == ("Pepemint", "Neon Blue", "Kitty Cat")
    assert lst.number == 2232
    assert lst.price_ton == 6598.99
    assert lst.price_str == "6598.99"
    assert lst.listed_at.year == 2026 and lst.listed_at.tzinfo is not None
    assert lst.tg_link == "https://t.me/nft/PlushPepe-2232"
    assert lst.raw is SEARCH_ITEM


def test_listing_from_api_tolerates_missing_fields():
    lst = Listing.from_api({"id": "a", "name": "Lol Pop", "price": None, "attributes": None})
    assert lst.price_ton == 0 and lst.model is None and lst.number is None and lst.listed_at is None


def test_parse_ts_variants():
    assert parse_ts("2026-10-28T18:46:02.3481Z").microsecond == 348100
    assert parse_ts("2026-10-28T18:46:02.123456789Z").microsecond == 123456
    assert parse_ts("garbage") is None
    assert parse_ts(None) is None


def test_short_name():
    assert short_name("Durov's Cap") == "durovscap"
    assert short_name("Jack-in-the-Box") == "jackinthebox"
    assert short_name("Plush Pepe") == "plushpepe"


def test_collection_info():
    c = CollectionInfo.from_api({"id": "u", "name": "Resistance Dog", "short_name": "resistancedog", "floor_price": "400"})
    assert c.floor_ton == 400 and c.short_name == "resistancedog"
    assert CollectionInfo.from_api({"id": "u", "name": "New Thing", "floor_price": None}).floor_ton is None


def test_attribute_floors():
    af = AttributeFloors.from_api(FILTERS)
    assert af.models == {"Cryo Pepe": 86.0, "Poopsicle": 6.8}
    assert af.canonical("models", "cryo pepe") == "Cryo Pepe"
    assert af.canonical("models", "nope") is None
    assert af.floor_for("Poopsicle", "Burgundy", None) == 6.8
    assert af.floor_for(None, "Burgundy", None) is None  # backdrop floors are global -> ignored
    assert af.floor_for("Poopsicle", None, "Astronaut") == 6.8
    assert af.floor_for(None, None, None) is None
    # legacy {name: floor} shape
    assert AttributeFloors.from_api({"models": {"A": "1.5"}}).models == {"A": 1.5}


def test_build_search_params():
    f = SearchFilters(collections=["Plush Pepe"], models=["Pepemint"], backdrops=["Neon Blue"],
                      sort=SORT_PRICE_ASC, limit=20)
    qs = urlencode(build_search_params(f, ["uuid-1"]), quote_via=quote_plus)
    assert qs.startswith("offset=0&limit=20&sort_by=price+asc&collection_ids=uuid-1")
    assert "filter_by_models=Pepemint" in qs
    assert "filter_by_backdrops=Neon+Blue" in qs
    assert "min_price" not in qs
    assert qs.endswith("status=listed&exclude_bundled=true&premarket_status=all")
    f2 = SearchFilters(sort=SORT_LATEST, max_price=3.5)
    qs2 = urlencode(build_search_params(f2, ["a", "b"]), quote_via=quote_plus)
    assert "sort_by=listed_at+desc" in qs2
    assert "collection_ids=a%2Cb" in qs2
    assert "min_price=0&max_price=3.5" in qs2


def test_extract_init_data():
    init = "query_id=AAE&user=%7B%22id%22%3A1%7D&auth_date=1730000000&hash=abc"
    url = f"https://portal-market.com/#tgWebAppData={quote(init, safe='')}&tgWebAppVersion=8.0&tgWebAppPlatform=android"
    assert extract_init_data(url) == init
    url_q = f"https://portal-market.com/?tgWebAppData={quote(init, safe='')}"
    assert extract_init_data(url_q) == init


def test_mask_never_leaks():
    secret = "query_id=AAE" + "x" * 200
    m = mask(secret)
    assert len(m) < 40 and secret not in m
    assert mask("") == "<empty>"
