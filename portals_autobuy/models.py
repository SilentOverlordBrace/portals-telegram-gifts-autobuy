"""Plain data models shared by client, rules and engine."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def short_name(name: str) -> str:
    """Portals collection short name: 'Durov's Cap' -> 'durovscap'."""
    return re.sub(r"[\s'’\-]", "", name or "").lower()


def norm(value: str | None) -> str:
    """Case/whitespace-insensitive key for comparing attribute names."""
    return " ".join((value or "").split()).casefold()


_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def alnum_key(value: str | None) -> str:
    """Lowercase alphanumerics only: 'Durov's Cap', 'DurovsCap' and a t.me/nft slug name all match."""
    return _NON_ALNUM.sub("", (value or "").lower())


def parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = _FRACTION_RE.sub(r"\1", value.strip())  # trim nanoseconds to micro
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_price(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True, slots=True)
class Listing:
    id: str
    collection: str
    model: str | None
    backdrop: str | None
    symbol: str | None
    number: int | None
    price_ton: float
    listed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Listing":
        """Parse one item of `GET /nfts/search` -> `results[]` (verified live 2026-10-28)."""
        attrs: dict[str, str] = {}
        for attr in data.get("attributes") or []:
            if isinstance(attr, dict) and attr.get("type"):
                attrs[str(attr["type"])] = attr.get("value")
        number = data.get("external_collection_number")
        try:
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            number = None
        return cls(
            id=str(data["id"]),
            collection=str(data.get("name") or ""),
            model=attrs.get("model"),
            backdrop=attrs.get("backdrop"),
            symbol=attrs.get("symbol"),
            number=number,
            price_ton=parse_price(data.get("price")),
            listed_at=parse_ts(data.get("listed_at")),
            raw=data,
        )

    @property
    def price_str(self) -> str:
        """Price exactly as the API sent it (used in the buy payload)."""
        raw_price = self.raw.get("price")
        return str(raw_price) if raw_price is not None else f"{self.price_ton:g}"

    @property
    def key(self) -> str:
        """Dedupe key: a relist / price change of the same gift is a new event."""
        return f"{self.id}:{self.price_str}"

    @property
    def title(self) -> str:
        return f"{self.collection} #{self.number}" if self.number is not None else self.collection

    @property
    def short_title(self) -> str:
        return self.title

    @property
    def slug(self) -> str | None:
        """t.me/nft slug: Portals sends it as `tg_id` (verified live, e.g. 'PoolFloat-130763')."""
        tg_id = (self.raw or {}).get("tg_id")
        if tg_id:
            return str(tg_id)
        link = self.tg_link
        return link.rsplit("/", 1)[-1] if link else None

    @property
    def url(self) -> str | None:
        slug = self.slug
        return f"https://t.me/nft/{slug}" if slug else None

    @property
    def traits(self) -> str:
        return " / ".join(x for x in (self.model, self.backdrop, self.symbol) if x) or "-"

    @property
    def tg_link(self) -> str | None:
        if self.number is None or not self.collection:
            return None
        slug = re.sub(r"[\s'’\-]", "", self.collection)
        return f"https://t.me/nft/{slug}-{self.number}"


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class CollectionInfo:
    id: str
    name: str
    short_name: str
    floor_ton: float | None
    # Fields verified public on 2026-10-28 (GET /collections, no auth).
    zero_commission: bool = False
    is_premarket: bool = False
    listed_count: int | None = None
    supply: int | None = None
    sales_24h: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "CollectionInfo":
        """Parse one item of `GET /collections` -> `collections[]` (verified live)."""
        floor = data.get("floor_price")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or ""),
            short_name=str(data.get("short_name") or short_name(data.get("name") or "")),
            floor_ton=parse_price(floor) if floor not in (None, "") else None,
            zero_commission=bool(data.get("zero_commission")),
            is_premarket=bool(data.get("is_premarket")),
            listed_count=_int(data.get("listed_count")),
            supply=_int(data.get("supply")),
            sales_24h=_int(data.get("sales_24h_count")),
        )


@dataclass(frozen=True, slots=True)
class AttributeFloors:
    """Per-attribute floors for one collection: {'models': {name: floor}, ...}."""

    models: dict[str, float] = field(default_factory=dict)
    backdrops: dict[str, float] = field(default_factory=dict)
    symbols: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "AttributeFloors":
        """Parse `GET /collections/filters?short_names=x` -> `collections[x]` (verified live)."""

        def conv(items: Any) -> dict[str, float]:
            out: dict[str, float] = {}
            if isinstance(items, dict):  # older shape: {name: floor}
                return {str(k): parse_price(v) for k, v in items.items()}
            for it in items or []:
                if isinstance(it, dict) and it.get("name"):
                    out[str(it["name"])] = parse_price(it.get("floor_price"))
            return out

        return cls(
            models=conv(data.get("models")),
            backdrops=conv(data.get("backdrops")),
            symbols=conv(data.get("symbols")),
        )

    @staticmethod
    def _find(table: dict[str, float], name: str | None) -> tuple[str, float] | None:
        if not name:
            return None
        key = norm(name)
        for k, v in table.items():
            if norm(k) == key:
                return k, v
        return None

    def canonical(self, kind: str, name: str) -> str | None:
        hit = self._find(getattr(self, kind), name)
        return hit[0] if hit else None

    def floor_for(self, model: str | None, backdrop: str | None, symbol: str | None) -> float | None:
        """Highest floor among the given traits: a lower bound for the floor of that combination.

        Backdrops are ignored: live data shows backdrop floors are marketplace-wide (same value
        for every collection, no "collection" field), so they say nothing about this collection.
        TODO(verify): re-check if Portals starts returning per-collection backdrop floors.
        """
        del backdrop
        vals = []
        for table, name in ((self.models, model), (self.symbols, symbol)):
            hit = self._find(table, name)
            if hit and hit[1] > 0:
                vals.append(hit[1])
        return max(vals) if vals else None
