"""Buy rules: what to snipe and at what price. `match()` / `BuyRule.match` are pure."""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .explain import plural
from .models import Listing, norm
from .pricing import CONFIDENCE, DealMath, conf_rank, legacy_deal

MODES = ("auto", "confirm", "watch")  # strictness grows left to right
EXITS = ("here", "best")
STATES = ("shadow", "live", "paused")
STATE_UNSET = ""  # a rules.json entry without "state"; Storage.replace_rules_from_source resolves it

# Reason codes for match(); human text lives next to them in MatchResult.reason.
OK = "OK"
REASON_CODES = (
    "DISABLED",
    "MISMATCH",
    "BAD_PRICE",
    "TRAP_BUNDLE",
    "TRAP_LUCKY",
    "LOCKED",
    "OVER_MAX",
    "LOW_CONFIDENCE",
    "LOW_DISCOUNT",
    "LOSES_AFTER_FEES",
    "LOW_PROFIT",
)


class RuleParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MatchResult:
    ok: bool
    reason: str
    discount_pct: float | None = None
    code: str = OK
    deal: DealMath | None = None


def discount_pct(price: float, floor: float | None) -> float | None:
    """How far below the floor the price is, in percent (negative = above floor)."""
    if floor is None or floor <= 0:
        return None
    return (floor - price) / floor * 100.0


def stricter_mode(*modes: str) -> str:
    return max(modes, key=lambda m: MODES.index(m) if m in MODES else 0)


@dataclass(slots=True)
class BuyRule:
    collection: str
    max_price_ton: float
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    min_discount_pct: float = 0.0
    id: int | None = None
    enabled: bool = True
    source: str = "bot"  # "bot" | "file" | "mock" | "demo"
    min_profit_ton: float = 0.0
    min_profit_pct: float = 0.0
    min_confidence: str = "med"  # low | med | high
    allow_locked_days: float = 0.0  # 0 = never buy locked gifts
    mode: str = "auto"  # auto | confirm | watch
    exit: str = "here"  # here | best
    state: str = "live"  # shadow | live | paused (shadow = practice-only even in LIVE)
    promoted_at: float | None = None

    def matches_attrs(self, listing: Listing) -> bool:
        if norm(listing.collection) != norm(self.collection):
            return False
        for want, have in (
            (self.model, listing.model),
            (self.backdrop, listing.backdrop),
            (self.symbol, listing.symbol),
        ):
            if want and norm(want) != norm(have):
                return False
        return True

    def match(self, listing: Listing, floor: "float | DealMath | None") -> MatchResult:
        """Decide whether `listing` should be bought under this rule (see `match`)."""
        return match(self, listing, floor)

    def filter_key(self) -> tuple[str, str, str, str]:
        """Rules with the same key share one API search per cycle."""
        return (norm(self.collection), norm(self.model), norm(self.backdrop), norm(self.symbol))

    def filter_key_str(self) -> str:
        return "|".join(self.filter_key())

    @property
    def pins_attributes(self) -> bool:
        return bool(self.model or self.backdrop or self.symbol)

    def describe(self) -> str:
        parts = [self.collection, f"max={self.max_price_ton:g}"]
        if self.model:
            parts.append(f"model={self.model}")
        if self.backdrop:
            parts.append(f"backdrop={self.backdrop}")
        if self.symbol:
            parts.append(f"symbol={self.symbol}")
        if self.min_discount_pct:
            parts.append(f"discount={self.min_discount_pct:g}%")
        if self.min_profit_ton:
            parts.append(f"profit={self.min_profit_ton:g}")
        if self.min_profit_pct:
            parts.append(f"profit%={self.min_profit_pct:g}")
        if self.min_confidence != "med":
            parts.append(f"conf={self.min_confidence}")
        if self.allow_locked_days:
            parts.append(f"locked={self.allow_locked_days:g}")
        if self.mode != "auto":
            parts.append(f"mode={self.mode}")
        if self.exit != "here":
            parts.append(f"exit={self.exit}")
        if self.state != "live":
            parts.append(f"[{self.state}]")
        if not self.enabled:
            parts.append("(disabled)")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "file") -> "BuyRule":
        if not isinstance(data, dict):
            raise RuleParseError(f"rule must be an object, got {type(data).__name__}")
        try:
            collection = str(data["collection"]).strip()
            max_price = data.get("max_price_ton", data.get("max"))
            if not collection or max_price is None:
                raise KeyError("collection/max_price_ton")
            max_price = float(max_price)
            if not math.isfinite(max_price) or max_price <= 0:  # same rule as /addrule; use enabled=false to switch off
                raise RuleParseError(f"rule for {collection!r}: max_price_ton must be > 0 (got {max_price:g}); "
                                     "set \"enabled\": false to switch a rule off")
            def finite(key: str, value: Any) -> float:
                v = float(value or 0)
                if not math.isfinite(v):  # NaN would make every comparison False and pass the check
                    raise RuleParseError(f"rule for {collection!r}: {key} must be a finite number (got {v:g})")
                return v

            return cls(
                collection=collection,
                max_price_ton=max_price,
                model=data.get("model") or None,
                backdrop=data.get("backdrop") or None,
                symbol=data.get("symbol") or None,
                min_discount_pct=finite("min_discount_pct", data.get("min_discount_pct", data.get("discount", 0))),
                enabled=bool(data.get("enabled", True)),
                source=source,
                min_profit_ton=finite("min_profit_ton", data.get("min_profit_ton", 0)),
                min_profit_pct=finite("min_profit_pct", data.get("min_profit_pct", 0)),
                min_confidence=_choice("min_confidence", str(data.get("min_confidence") or "med"), CONFIDENCE),
                allow_locked_days=finite("allow_locked_days", data.get("allow_locked_days", 0)),
                mode=_choice("mode", str(data.get("mode") or "auto"), MODES),
                exit=_choice("exit", str(data.get("exit") or "here"), EXITS),
                # No "state" in the file: STATE_UNSET. The loader starts a new rule in shadow (SHADOW_NEW_RULES)
                # and keeps the stored state (a promotion, a pause) of an existing one.
                state=_choice("state", str(data["state"]), STATES) if data.get("state") else STATE_UNSET,
            )
        except RuleParseError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise RuleParseError(f"bad rule {data!r}: {exc}") from exc


def match(rule: BuyRule, listing: Listing, floor_ton: "float | DealMath | None" = None) -> MatchResult:
    """Pure decision. Order: attributes, blocking flags, lock, max price on cost, confidence,
    discount, profit. `floor_ton` is a DealMath (net math) or, for old callers, a bare floor."""
    deal = floor_ton if isinstance(floor_ton, DealMath) else legacy_deal(listing, floor_ton)
    disc = deal.discount_pct

    def no(code: str, text: str) -> MatchResult:
        return MatchResult(False, text, disc, code, deal)

    if not rule.enabled:
        return no("DISABLED", "rule disabled")
    if norm(rule.collection) != norm(listing.collection):
        return no("MISMATCH", "collection mismatch")
    for attr in ("model", "backdrop", "symbol"):
        wanted = getattr(rule, attr)
        if wanted and norm(wanted) != norm(getattr(listing, attr)):
            return no("MISMATCH", f"{attr} mismatch")
    if listing.price_ton <= 0:
        return no("BAD_PRICE", "invalid price")
    for flag in deal.blocking:
        code = flag.code if flag.code in REASON_CODES else "BAD_PRICE"
        return no(code, flag.text)
    if deal.has("LOCKED"):
        days = deal.lock_days
        if rule.allow_locked_days <= 0 or days is None or days > rule.allow_locked_days:
            flag = deal.flag("LOCKED")
            return no("LOCKED", f"locked: {flag.text if flag else 'unlock date unknown'}")
    if deal.cost > rule.max_price_ton + 1e-9:
        if abs(deal.cost - deal.price) < 1e-9:
            return no("OVER_MAX", f"price {deal.price:g} > max {rule.max_price_ton:g}")
        return no("OVER_MAX", f"cost {deal.cost:.2f} incl. fees > max {rule.max_price_ton:g}")
    if conf_rank(deal.confidence) < conf_rank(rule.min_confidence):
        return no("LOW_CONFIDENCE", f"floor confidence {deal.confidence} < {rule.min_confidence} "
                                    f"({deal.floor_source}, {plural(deal.n, 'comparable')})")
    if rule.min_discount_pct > 0:
        if disc is None:
            return no("LOW_DISCOUNT", "no floor to compare with")
        if disc < rule.min_discount_pct:
            return no("LOW_DISCOUNT", f"discount {disc:.1f}% < required {rule.min_discount_pct:g}%")
    if deal.profit_ton is None:
        if rule.min_profit_ton > 0 or rule.min_profit_pct > 0:
            return no("LOW_PROFIT", "no floor to estimate profit")
    elif deal.profit_ton < 0:
        return no("LOSES_AFTER_FEES", f"loses {abs(deal.profit_ton):.2f} TON after fees")
    elif deal.profit_ton < rule.min_profit_ton:
        return no("LOW_PROFIT", f"profit {deal.profit_ton:+.2f} TON < {rule.min_profit_ton:g} TON")
    elif rule.min_profit_pct > 0 and (deal.profit_pct or 0) < rule.min_profit_pct:
        return no("LOW_PROFIT", f"profit {deal.profit_pct or 0:+.1f}% < {rule.min_profit_pct:g}%")
    return MatchResult(True, "match", disc, OK, deal)


@dataclass(slots=True, frozen=True)
class NearMiss:
    check: str  # max | discount | profit
    value: float
    limit: float
    miss_pct: float
    new_max: float | None = None

    def text(self) -> str:
        if self.check == "max":
            return f"Missed by a hair: {self.value:.2f} TON, your max is {self.limit:.2f}"
        if self.check == "discount":
            return f"Missed by a hair: discount {self.value:.1f}%, your minimum is {self.limit:g}%"
        return f"Missed by a hair: profit {self.value:+.2f} TON, your minimum is {self.limit:g} TON"


def near_miss(rule: BuyRule, listing: Listing, deal: DealMath, *, pct: float = 5.0) -> NearMiss | None:
    """A listing that fails exactly one numeric check (max on cost, discount or profit) by <= pct."""
    relaxed = replace(rule, max_price_ton=math.inf, min_discount_pct=0.0, min_profit_ton=0.0, min_profit_pct=0.0)
    if not match(relaxed, listing, deal).ok:
        return None
    misses: list[NearMiss] = []
    if deal.cost > rule.max_price_ton + 1e-9 and rule.max_price_ton > 0:  # a 0 max (old row) is no near miss
        new_max = math.ceil(round(deal.cost / 0.05, 6)) * 0.05
        misses.append(NearMiss("max", deal.cost, rule.max_price_ton,
                               (deal.cost - rule.max_price_ton) / rule.max_price_ton * 100, round(new_max, 2)))
    if rule.min_discount_pct > 0 and deal.discount_pct is not None and deal.discount_pct < rule.min_discount_pct:
        misses.append(NearMiss("discount", deal.discount_pct, rule.min_discount_pct,
                               (rule.min_discount_pct - deal.discount_pct) / rule.min_discount_pct * 100))
    if rule.min_profit_ton > 0 and deal.profit_ton is not None and deal.profit_ton < rule.min_profit_ton:
        misses.append(NearMiss("profit", deal.profit_ton, rule.min_profit_ton,
                               (rule.min_profit_ton - deal.profit_ton) / rule.min_profit_ton * 100))
    if len(misses) != 1 or misses[0].miss_pct > pct:
        return None
    return misses[0]


# --- /addrule parsing -------------------------------------------------------

_KEYS = {
    "max": "max_price_ton",
    "price": "max_price_ton",
    "max_price": "max_price_ton",
    "max_price_ton": "max_price_ton",
    "model": "model",
    "backdrop": "backdrop",
    "bg": "backdrop",
    "symbol": "symbol",
    "discount": "min_discount_pct",
    "disc": "min_discount_pct",
    "min_discount_pct": "min_discount_pct",
    "profit": "min_profit_ton",
    "min_profit": "min_profit_ton",
    "min_profit_ton": "min_profit_ton",
    "profit%": "min_profit_pct",
    "min_profit_pct": "min_profit_pct",
    "conf": "min_confidence",
    "confidence": "min_confidence",
    "min_confidence": "min_confidence",
    "locked": "allow_locked_days",
    "allow_locked_days": "allow_locked_days",
    "mode": "mode",
    "exit": "exit",
}


def _choice(key: str, value: str, allowed: tuple[str, ...]) -> str:
    value = value.strip().lower()
    if value not in allowed:
        raise RuleParseError(f"{key} must be one of {'|'.join(allowed)}, got {value!r}")
    return value


def parse_addrule_args(text: str) -> BuyRule:
    """Parse `<collection> max=<ton> [model=..] [backdrop=..] [symbol=..] [discount=<pct>]
    [profit=<ton>] [profit%=<pct>] [conf=low|med|high] [locked=<days>] [mode=auto|confirm|watch] [exit=here|best]`.

    Multi-word values work with or without quotes: `Plush Pepe max=5 backdrop=Neon Blue`.
    """
    try:
        tokens = shlex.split(text or "")
    except ValueError:
        tokens = (text or "").split()
    coll_words: list[str] = []
    values: dict[str, list[str]] = {}
    current: str | None = None
    for tok in tokens:
        key, sep, val = tok.partition("=")
        if sep and key.lower() in _KEYS:
            current = _KEYS[key.lower()]
            values[current] = [val] if val else []
        elif current is None:
            coll_words.append(tok)
        else:
            values[current].append(tok)
    collection = " ".join(coll_words).strip()
    if not collection:
        raise RuleParseError("collection name is required")
    if "max_price_ton" not in values:
        raise RuleParseError("max=<ton> is required")

    def num(field: str, label: str | None = None) -> float:
        raw = " ".join(values[field]).replace(",", ".").rstrip("%").strip()
        try:
            v = float(raw)
        except ValueError as exc:
            raise RuleParseError(f"{label or field} must be a number, got {raw!r}") from exc
        if not math.isfinite(v):  # nan/inf would slip past every limit comparison
            raise RuleParseError(f"{label or field} must be a finite number, got {raw!r}")
        if v < 0:
            raise RuleParseError(f"{label or field} must be >= 0")
        return v

    def opt_num(field: str, label: str) -> float:
        return num(field, label) if field in values else 0.0

    max_price = num("max_price_ton", "max")
    if max_price <= 0:
        raise RuleParseError("max must be > 0")
    discount = num("min_discount_pct", "discount") if "min_discount_pct" in values else 0.0
    if discount >= 100:
        raise RuleParseError("discount must be < 100")

    def txt(field: str) -> str | None:
        v = " ".join(values.get(field, [])).strip()
        return v or None

    return BuyRule(
        collection=collection,
        max_price_ton=max_price,
        model=txt("model"),
        backdrop=txt("backdrop"),
        symbol=txt("symbol"),
        min_discount_pct=discount,
        min_profit_ton=opt_num("min_profit_ton", "profit"),
        min_profit_pct=opt_num("min_profit_pct", "profit%"),
        min_confidence=_choice("conf", txt("min_confidence") or "med", CONFIDENCE),
        allow_locked_days=opt_num("allow_locked_days", "locked"),
        mode=_choice("mode", txt("mode") or "auto", MODES),
        exit=_choice("exit", txt("exit") or "here", EXITS),
    )


# --- rules.json ---------------------------------------------------------------


def file_rule_keys(rules: list[BuyRule]) -> list[str]:
    """Stable identity of each rules.json entry: its filter key, plus '#2', '#3'... for a repeated filter.
    Changing max, discount, mode etc. keeps the rule's id; changing collection/model/backdrop/symbol makes a new rule."""
    seen: dict[str, int] = {}
    keys = []
    for rule in rules:
        base = rule.filter_key_str()
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def load_rules_file(path: str | Path) -> list[BuyRule]:
    """Load rules from JSON: a list of rule objects, or {"rules": [...]}. Missing file -> []."""
    p = Path(path)
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise RuleParseError(f"{p}: expected a JSON list of rules")
    return [BuyRule.from_dict(item, source="file") for item in data]
