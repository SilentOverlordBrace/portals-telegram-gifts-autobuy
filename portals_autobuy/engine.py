"""Poll loop: fetch -> dedupe -> robust floor -> net deal math -> match -> card -> (practice) buy with limits.

Safety: every spend (auto-buy or tap, practice or live) goes through Engine._spend, which holds one
lock while it re-reads every switch (panic, pause, practice, rule pauses), checks the limits on all-in
cost (MAX_PRICE_TON, rolling daily budget, hourly cap) against fresh totals, buys and records the
purchase, so concurrent taps and the engine loop can never jointly exceed a limit. Live buys re-read
the switches once more right before client.buy(), after the balance call. DRY_RUN=true in .env is a
hard lock no command or stored setting can lift; LIVE is armed for LIVE_ARM_HOURS only; /panic stops
everything until /resume + /dry off.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .client import (
    SORT_LATEST,
    SORT_PRICE_ASC,
    MarketClient,
    PortalsError,
    RateLimited,
    SearchFilters,
    UnknownCollection,
    classify_buy_response,
)
from .config import ConfigError, Settings
from .events import BOT_NAME, Event, EventLog
from .explain import explain_more, llm_available, local_clock, severity, suite_code, why_text
from .models import AttributeFloors, CollectionInfo, Listing, norm, short_name
from .notifier import (
    DealView,
    Notifier,
    fill_status,
    format_buy_failed,
    format_buy_ok,
    format_skip,
    render_deal_card,
    render_near_miss,
)
from .peers import Peers, PeerView, best_exit, build_snapshot, guards, write_snapshot
from .pricing import (
    DealMath,
    FeeModel,
    FloorEstimate,
    assess,
    classify,
    estimate_floor,
    fee_model_from_settings,
    too_good_flag,
)
from .rules import BuyRule, MatchResult, load_rules_file, match, near_miss, stricter_mode
from .safety import SafetyState, balance_mismatch, floor_crash, losing_streak
from .storage import PendingCard, Storage
from .tg_auth import TgAuthError

log = logging.getLogger(__name__)

DAY = 86400.0
HOUR = 3600.0
MARKET = "portals"
MARKET_TITLE = "Portals"
MUTE_SEC = 24 * HOUR
GATE_TTL_SEC = 600.0  # a [Go live for 24 h] button works once, within 10 min of its /dry off report
RESUME_TTL_SEC = 600.0  # a [Confirm resume] / [Resume] button works once, within 10 min, until the next stop
GATE_SLACK_SEC = 300.0  # a stored live_until further ahead than LIVE_ARM_HOURS plus this is not a valid gate
ERROR_ALERT_AFTER = 5  # consecutive failed cycles before admins are pinged


# ---------------------------------------------------------------- pure helpers
@dataclass(slots=True, frozen=True)
class Limits:
    max_price_ton: float
    daily_budget_ton: float
    max_buys_per_hour: int

    @classmethod
    def from_settings(cls, s: Settings) -> "Limits":
        return cls(s.MAX_PRICE_TON, s.DAILY_BUDGET_TON, s.MAX_BUYS_PER_HOUR)


def check_limits_detail(cost_ton: float, limits: Limits, *, spent_24h: float, buys_last_hour: int) -> tuple[str, str] | None:
    """(reason code, text) if a buy costing `cost_ton` all-in must be blocked, else None.

    Fails closed: each check is written as "allowed only if <= limit", so a NaN anywhere (a cost, a total
    or a limit) blocks the buy instead of slipping through a comparison that is always False."""
    if not cost_ton <= limits.max_price_ton + 1e-9:
        return "LIMIT_MAX_PRICE", f"price {cost_ton:g} TON > MAX_PRICE_TON {limits.max_price_ton:g}"
    if not spent_24h + cost_ton <= limits.daily_budget_ton + 1e-9:
        return "LIMIT_DAILY_BUDGET", (
            f"daily budget: spent {spent_24h:g} + {cost_ton:g} > DAILY_BUDGET_TON {limits.daily_budget_ton:g}"
        )
    if not buys_last_hour < limits.max_buys_per_hour:
        return "LIMIT_HOURLY", f"MAX_BUYS_PER_HOUR {limits.max_buys_per_hour} reached"
    return None


def backoff_delay(consecutive_errors: int, base: float, cap: float) -> float:
    if consecutive_errors <= 0:
        return base
    return min(cap, max(base, 1.0) * (2 ** consecutive_errors))


def mute_keys(collection: str, model: str | None) -> list[str]:
    keys = [norm(collection)]
    if model:
        keys.append(f"{norm(collection)}:{norm(model)}")
    return keys


def is_floor_pool_member(listing: Listing, now: float) -> bool:
    """Traps and locked gifts are not comparables: they would drag the floor down."""
    return not any(f.blocking or f.code == "LOCKED" for f in classify(listing, MARKET, now))


def buy_outcome_unknown(exc: BaseException) -> bool:
    """A buy() that raised may still have executed on the server: network errors and timeouts (no status),
    5xx, and a 2xx with an unreadable body. A 4xx (incl. 429) is a definite refusal."""
    if not isinstance(exc, PortalsError):
        return True
    status = exc.status
    return status is None or status >= 500 or 200 <= status < 300


# ---------------------------------------------------------------- engine
@dataclass(slots=True)
class Candidate:
    listing: Listing
    rule: BuyRule
    floor: float | None
    discount: float | None


@dataclass(slots=True)
class CycleReport:
    fetched: int = 0
    new: int = 0
    matched: int = 0
    rejected: int = 0
    bought: int = 0
    dry_bought: int = 0
    skipped: int = 0
    failed: int = 0
    alerts: int = 0
    paused: bool = False
    rules: int = 0
    rule_errors: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        if self.paused:
            return "paused"
        return (
            f"fetched={self.fetched} new={self.new} matched={self.matched} rejected={self.rejected} "
            f"bought={self.bought} dry={self.dry_bought} cards={self.alerts} skipped={self.skipped} failed={self.failed}"
        )

    def top_reasons(self, n: int = 3) -> str:
        return " · ".join(f"{k} {v}" for k, v in self.reasons.most_common(n)) or "none"


@dataclass(slots=True)
class EngineStats:
    cycles: int = 0
    last_cycle_at: float | None = None
    last_ok_at: float | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    current_delay: float = 0.0
    api_errors: int = 0


@dataclass(slots=True)
class TapResult:
    ok: bool
    text: str
    status: str | None = None
    reply: str | None = None  # longer text for a chat reply (Why?, Explain more)


@dataclass(slots=True)
class Spend:
    """What Engine._spend did with one buy attempt.
    kind: refused (a switch or breaker said no) | blocked (a limit) | practice | bought | error | unknown."""

    kind: str
    code: str = "OK"
    text: str = ""
    practice: bool = False
    elapsed: float = 0.0


class Engine:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        client: MarketClient,
        notifier: Notifier,
        *,
        force_dry: bool = False,
        verbose_rejects: bool = False,
        clock: Callable[[], float] = time.time,
        peers: Peers | None = None,
        events: EventLog | None = None,
        snapshot_path: str | Path | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.client = client
        self.notifier = notifier
        self.force_dry = force_dry
        self.clock = clock
        self.peers = peers
        self.events = events
        self._reject_level = logging.INFO if verbose_rejects else logging.DEBUG
        self.stats = EngineStats(current_delay=settings.POLL_INTERVAL)
        self.last_report: CycleReport | None = None
        self.fees = fee_model_from_settings(settings)
        # The CLI passes DATA_DIR/floors_portals.json; engines built in tests write no snapshot file.
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        try:
            self.admin_ids: set[int] = set(settings.admin_ids)
        except ConfigError:
            self.admin_ids = set()
        self._warned_rules: set[int | None] = set()
        self._floors: dict[str, float] = {}  # {short_name: floor}
        self._meta: dict[str, CollectionInfo] = {}
        self._zero_commission: set[str] = set()
        self._snap_written: dict[str, float] = {}
        self._near_times: deque[float] = deque()
        self._last_snapshot_file = -1e18
        self.consecutive_buy_errors = 0
        # One lock around switch re-check + limit check + buy + record, for the loop and every tap alike.
        self._spend_lock = asyncio.Lock()
        self._closing = False  # set by drain(): no new spend starts, the one in flight finishes
        self._peers_deadline: float | None = None  # monotonic; one PEERS_TIMEOUT_SEC budget per cycle
        self._rate_task: asyncio.Task | None = None
        self._stale_warned = False
        self.last_mark_at = -1e18
        self.last_tick_at = -1e18

    # ----- runtime switches (persisted in settings table) -----
    @property
    def live_locked(self) -> bool:
        """DRY_RUN=true in .env (or mock mode) is a hard lock: no command or stored setting can enable live buys."""
        return self.force_dry or self.settings.DRY_RUN

    async def is_paused(self) -> bool:
        return bool(await self.storage.get_flag("paused"))

    async def set_paused(self, value: bool) -> None:
        await self.storage.set_flag("paused", value)
        if value:  # a new stop (/pause, /panic, a breaker) voids every older [Confirm resume] / [Resume] button
            await self.storage.delete_setting("resume_nonce")

    async def is_panic(self) -> bool:
        return bool(await self.storage.get_flag("panic"))

    async def live_until(self) -> float | None:
        return await self.storage.get_float("live_until")

    async def is_dry(self, now: float | None = None) -> bool:
        """Practice unless: no .env lock, no panic, and LIVE armed with a valid live_until in the future.

        Fails closed: a missing or unreadable live_until, a non-finite one, or one further ahead than
        LIVE_ARM_HOURS (a clock jump, a hand-edited row) is practice, and so is a missing `dry` flag. An
        Engine on a fresh database with DRY_RUN=false is therefore in practice until arm_live()."""
        if self.live_locked or await self.is_panic():
            return True
        now = self.clock() if now is None else now
        until = await self.live_until()
        if until is None:  # fail closed: LIVE only ever exists as an armed, expiring gate (arm_live)
            return True
        if not math.isfinite(until) or until - now > self.settings.LIVE_ARM_HOURS * HOUR + GATE_SLACK_SEC:
            log.warning("live_until %r is not a valid live gate: back to practice", until)
            await self._end_live(now, "Live mode ended: the stored live gate was not valid")
            return True
        if now > until:
            await self._end_live(now)
            return True
        flag = await self.storage.get_flag("dry")
        return bool(flag) if flag is not None else True

    async def _end_live(self, now: float, why: str | None = None) -> None:
        await self.storage.set_flag("dry", True)
        await self.storage.delete_setting("live_until")
        why = why or f"Live mode ended after {self.settings.LIVE_ARM_HOURS:g} h"
        await self.notifier.send(f"{why}; back to practice. /dry off to renew.")
        self._emit_state("pause", now, "live gate expired")

    async def set_dry(self, value: bool) -> str | None:
        """Returns an error message when the change is not allowed. Going live always arms the gate."""
        if value:
            await self.storage.set_flag("dry", True)
            await self.storage.delete_setting("live_until")
            await self.storage.delete_setting("gate_nonce")
            return None
        return await self.arm_live()

    async def _issue_nonce(self, key: str, ttl: float, now: float | None = None) -> str:
        """Store a fresh one-time token under `key` (replacing any older one) and return it."""
        now = self.clock() if now is None else now
        nonce = secrets.token_urlsafe(6)
        await self.storage.set_setting(key, f"{nonce}:{now + ttl}")
        return nonce

    async def _consume_nonce(self, key: str, nonce: str, now: float | None = None) -> bool:
        """True once, for the newest unexpired token stored under `key`."""
        now = self.clock() if now is None else now
        stored = await self.storage.get_setting(key)
        if not stored or not nonce:
            return False
        value, _, until = stored.rpartition(":")
        if value != nonce or not await self.storage.take_setting(key, stored):
            return False
        try:
            return now <= float(until)
        except ValueError:
            return False

    async def issue_gate_nonce(self, now: float | None = None) -> str:
        """One-time token for the [Go live for 24 h] button of the /dry off report just sent."""
        return await self._issue_nonce("gate_nonce", GATE_TTL_SEC, now)

    async def consume_gate_nonce(self, nonce: str, now: float | None = None) -> bool:
        """True once for the newest, unexpired go-live button; /panic and /dry on void it."""
        return await self._consume_nonce("gate_nonce", nonce, now)

    async def issue_resume_nonce(self, now: float | None = None) -> str:
        """One-time token for a [Confirm resume] (/resume) or [Resume] (balance breaker) button."""
        return await self._issue_nonce("resume_nonce", RESUME_TTL_SEC, now)

    async def resume_with(self, nonce: str, now: float | None = None) -> bool:
        """A resume button: resumes only for the newest, unexpired one. Any later stop (/pause, /panic, a
        breaker) voids it, so an old button can never undo a newer /panic."""
        now = self.clock() if now is None else now
        if not await self._consume_nonce("resume_nonce", nonce, now):
            return False
        await self.resume(now)
        return True

    async def issue_rule_resume_nonce(self, rule_id: int, until: float, now: float | None = None) -> str:
        """One-time token for the [Resume] button of a floor-crash card; the next crash pause of the rule
        replaces it, so an old card cannot undo a newer pause. It is useless once the pause has run out."""
        now = self.clock() if now is None else now
        return await self._issue_nonce(f"rule_resume_nonce:{rule_id}", max(0.0, until - now), now)

    async def resume_rule_with(self, rule_id: int, nonce: str, now: float | None = None) -> bool:
        """[Resume] on a floor-crash card: clears that rule's crash pause, only for its newest card."""
        if not await self._consume_nonce(f"rule_resume_nonce:{rule_id}", nonce, now):
            return False
        await self.storage.delete_setting(f"rule_paused_until:{rule_id}")
        return True

    async def mode_line(self, now: float | None = None) -> str:
        """The mode buying runs in right now, in one sentence (the answer after a resume)."""
        now = self.clock() if now is None else now
        if not await self.is_dry(now):
            until = await self.live_until()
            when = f" until {local_clock(until, self.settings.TZ_OFFSET_HOURS)}" if until else ""
            return f"Mode: LIVE{when}: real TON is spent. /dry on switches to practice."
        if self.live_locked:
            return "Mode: PRACTICE (DRY_RUN=true in .env): nothing is really bought."
        return "Mode: PRACTICE until a new /dry off: nothing is really bought."

    async def arm_live(self, now: float | None = None) -> str | None:
        if self.live_locked:
            return "Live mode is locked: DRY_RUN=true in .env. Set DRY_RUN=false and restart to allow /dry off."
        if await self.is_panic():
            return "Everything is stopped by /panic. Use /resume first, then /dry off."
        now = self.clock() if now is None else now
        # The expiring gate first: a cleared dry flag without live_until never reads as LIVE (is_dry).
        await self.storage.set_setting("live_until", now + self.settings.LIVE_ARM_HOURS * HOUR)
        await self.storage.set_flag("dry", False)
        await self._expire_practice_cards()
        return None

    async def _expire_practice_cards(self) -> int:
        """LIVE was just armed: Buy buttons on cards sent in practice stop working at once, so no tap lands
        under a PRACTICE header while TON is real. A card without the flag counts as practice."""
        expired = 0
        for card in await self.storage.list_pending(("pending",)):
            if card.payload.get("practice") is False:
                continue
            if await self.storage.set_pending_status(card.token, "expired", only_from=("pending",)):
                await self._edit_card(card, "Expired: LIVE was armed after this PRACTICE card")
                expired += 1
        return expired

    async def start_in_practice(self, now: float | None = None) -> None:
        """Process start: LIVE only survives a restart while its 24 h gate is still open."""
        now = self.clock() if now is None else now
        until = await self.live_until()
        if until is None or now > until:
            await self.storage.set_flag("dry", True)
            await self.storage.delete_setting("live_until")

    async def panic(self, now: float | None = None) -> int:
        """Stop all buying at once: pause, practice on, live gate cleared, pending cards expired.
        Taps already executing are flagged too, so they refuse at their last check before buy()."""
        now = self.clock() if now is None else now
        await self.storage.set_flag("panic", True)
        await self.set_paused(True)
        await self.storage.set_flag("dry", True)
        await self.storage.delete_setting("live_until")
        await self.storage.delete_setting("gate_nonce")
        await self.storage.cancel_executing()
        cards = await self.storage.expire_pending(None)
        for card in cards:
            await self._edit_card(card, "Expired")
        await self.notifier.send("Everything stopped. Practice mode is forced until /resume (with confirm) and a new /dry off.")
        self._emit_state("pause", now, "panic")
        return len(cards)

    async def resume(self, now: float | None = None) -> None:
        """Confirmed /resume: clears panic and pause. After /panic the mode is practice until a new /dry off;
        after /pause or a breaker, a LIVE window that is still open keeps buying (mode_line says which)."""
        now = self.clock() if now is None else now
        await self.storage.set_flag("panic", False)
        await self.set_paused(False)
        self._emit_state("resume", now, "resume")

    async def effective_limits(self) -> Limits:
        """.env limits, tightened by a /preset if one is stored (a preset can never raise them)."""
        overrides = await self.storage.settings_with_prefix("limit:")

        def val(key: str, env: float) -> float:
            try:
                value = float(overrides[f"limit:{key}"]) if f"limit:{key}" in overrides else env
            except ValueError:
                return env
            return min(env, value) if math.isfinite(value) else env

        return Limits(
            val("MAX_PRICE_TON", self.settings.MAX_PRICE_TON),
            val("DAILY_BUDGET_TON", self.settings.DAILY_BUDGET_TON),
            int(val("MAX_BUYS_PER_HOUR", self.settings.MAX_BUYS_PER_HOUR)),
        )

    async def safety_state(self, now: float | None = None) -> SafetyState:
        now = self.clock() if now is None else now
        dry = await self.is_dry(now)
        limits = await self.effective_limits()
        return SafetyState(
            live=not dry,
            locked=self.live_locked,
            live_until=await self.live_until(),
            panic=await self.is_panic(),
            paused=await self.is_paused(),
            spent_24h=await self.storage.spent_since(now - DAY, dry=dry),
            buys_last_hour=await self.storage.buys_since(now - HOUR, dry=dry),
            max_price=limits.max_price_ton,
            daily_budget=limits.daily_budget_ton,
            max_per_hour=limits.max_buys_per_hour,
        )

    async def sync_rules_file(self, path: str | None = None) -> int:
        """rules.json is the source of truth for file rules; ids stay stable and a new file rule starts in shadow
        when SHADOW_NEW_RULES is on (see Storage.replace_rules_from_source)."""
        try:
            rules = load_rules_file(path or self.settings.RULES_FILE)
        except ValueError as exc:  # RuleParseError / JSONDecodeError
            raise ConfigError(f"RULES_FILE {path or self.settings.RULES_FILE}: {exc}") from exc
        n = await self.storage.replace_rules_from_source(
            "file", rules, new_state="shadow" if self.settings.SHADOW_NEW_RULES else "live")
        if n:
            log.info("Loaded %d rule(s) from %s", n, path or self.settings.RULES_FILE)
        return n

    # ----- fees -----
    def fees_for(self, collection: str) -> FeeModel:
        """Portals: collections flagged zero_commission on public /collections have no sell fee."""
        if short_name(collection) in self._zero_commission:
            return self.fees.with_sell(0.0)
        return self.fees

    def _learn_fees(self, meta: dict[str, CollectionInfo]) -> None:
        for key, info in meta.items():
            if info.zero_commission and key not in self._zero_commission:
                self._zero_commission.add(key)
                log.info("Portals sell fee for %s: 0%% (zero_commission)", info.name)
            elif not info.zero_commission and key in self._zero_commission:
                self._zero_commission.discard(key)
                log.info("Portals sell fee for %s: back to %g%%", info.name, self.fees.sell_pct)

    # ----- data -----
    async def api_floors(self) -> dict[str, float]:
        """{short_name: floor} (the client caches /collections for FLOORS_TTL)."""
        self._floors = await self.client.collection_floors()
        return self._floors

    async def _attrs(self, collection: str, cache: dict[str, AttributeFloors | None]) -> AttributeFloors | None:
        key = short_name(collection)
        if key not in cache:
            try:
                cache[key] = await self.client.attribute_floors(collection)
            except PortalsError as exc:
                log.debug("attribute floors for %s unavailable: %s", collection, exc)
                cache[key] = None
        return cache[key]

    async def _filters_for(self, rule: BuyRule, cache: dict[str, AttributeFloors | None]) -> SearchFilters:
        af = None
        if rule.pins_attributes:
            af = await self._attrs(rule.collection, cache)

        def canon(kind: str, value: str | None) -> list[str]:
            if not value:
                return []
            return [(af.canonical(kind, value) if af else None) or value]

        return SearchFilters(
            collections=[rule.collection],
            models=canon("models", rule.model),
            backdrops=canon("backdrops", rule.backdrop),
            symbols=canon("symbols", rule.symbol),
            sort=SORT_PRICE_ASC,
            limit=self.settings.SEARCH_LIMIT,
        )

    def _api_floor(self, rule: BuyRule, floors: dict[str, float], attrs: dict[str, AttributeFloors | None]) -> float | None:
        """Fallback when no comparable is listed: AttributeFloors.floor_for for pinned rules, else the collection floor."""
        if rule.pins_attributes:
            af = attrs.get(short_name(rule.collection))
            value = af.floor_for(rule.model, rule.backdrop, rule.symbol) if af else None
            if value:
                return value
        return floors.get(short_name(rule.collection))

    async def _resolve(self, rules: list[BuyRule], report: CycleReport) -> list[BuyRule]:
        valid: list[BuyRule] = []
        for rule in rules:
            try:
                await self.client.resolve_collection(rule.collection)
                valid.append(rule)
            except UnknownCollection as exc:
                report.rule_errors.append(f"rule #{rule.id}: {exc}")
                if rule.id not in self._warned_rules:
                    self._warned_rules.add(rule.id)
                    log.warning("rule #%s skipped: %s", rule.id, exc)
                    await self.notifier.send(f"Rule #{rule.id} skipped: {exc}")
        return valid

    # ----- one cycle -----
    async def run_cycle(self) -> CycleReport:
        report = CycleReport()
        now = self.clock()
        self.stats.cycles += 1
        self.stats.last_cycle_at = now
        self.last_report = report
        if await self.is_paused():
            report.paused = True
            return report
        rules = [r for r in await self.storage.list_rules(only_enabled=True) if r.state != "paused"]
        preset_min = await self.storage.get_float("limit:MIN_DISCOUNT_PCT")
        if preset_min:  # a /preset sets a global minimum discount on top of every rule
            rules = [replace(r, min_discount_pct=max(r.min_discount_pct, preset_min)) for r in rules]
        report.rules = len(rules)
        if not rules:
            log.debug("no active rules")
            self.stats.last_ok_at = now
            return report

        floors = await self.api_floors()
        self._meta = await self.client.collection_meta()  # same cached /collections call
        self._learn_fees(self._meta)
        rules = await self._resolve(rules, report)
        if not rules:
            self.stats.last_ok_at = now
            return report

        # 1) Fresh listings across all rule collections (one request).
        # TODO(verify): "listed_at desc" returns the newest listings but not strictly ordered; we don't rely on order.
        colls = sorted({rule.collection for rule in rules}, key=str.lower)
        fresh = await self.client.search_listings(
            SearchFilters(collections=colls, sort=SORT_LATEST, limit=self.settings.SEARCH_LIMIT)
        )
        # 2) Cheapest comparable listings per rule filter (one request per distinct filter).
        attrs: dict[str, AttributeFloors | None] = {}
        groups: dict[tuple, BuyRule] = {}
        for rule in rules:
            groups.setdefault(rule.filter_key(), rule)
        books: dict[tuple, list[Listing]] = {}
        fetched: list[Listing] = list(fresh)
        candidates: dict[str, Listing] = {}
        for key, rule in groups.items():
            cheap = await self.client.search_listings(await self._filters_for(rule, attrs))
            fetched.extend(cheap)
            book: dict[str, Listing] = {x.id: x for x in cheap if rule.matches_attrs(x)}
            for x in fresh:
                if rule.matches_attrs(x):
                    book.setdefault(x.id, x)
            books[key] = list(book.values())
            for x in book.values():
                candidates.setdefault(x.key, x)
        report.fetched = len(candidates)
        await self.storage.upsert_observations(fetched, now)  # no extra requests: data we already have
        pools = {k: [x for x in v if is_floor_pool_member(x, now)] for k, v in books.items()}
        api = {rule.filter_key(): self._api_floor(rule, floors, attrs) for rule in rules}
        snaps = await self._snapshot_floors(rules, pools, api, now)
        paused_rules = await self._crash_breaker(rules, snaps, now)
        await self._write_own_snapshot(floors, pools, attrs, now)
        cycle_mode = "dry" if await self.is_dry(now) else "live"  # for events.jsonl
        self._refresh_rate()

        # 3) Evaluate only listings not seen before at this price, cheapest first.
        self._peers_deadline = time.monotonic() + self.settings.PEERS_TIMEOUT_SEC
        try:
            for listing in sorted(candidates.values(), key=lambda x: x.price_ton):
                if await self.is_paused():  # /pause or a breaker (balance mismatch, failed buys) fired mid-cycle
                    log.info("paused mid-cycle: the remaining listings wait for /resume")
                    break
                if not await self.storage.mark_seen(listing):
                    continue
                report.new += 1
                await self._evaluate(listing, rules, pools, api, snaps, paused_rules, report, now, cycle_mode)
        finally:
            self._peers_deadline = None
        self.stats.last_ok_at = now
        log.debug("cycle %d (%s): %s", self.stats.cycles, "PRACTICE" if cycle_mode == "dry" else "LIVE",
                  report.summary())
        return report

    async def _snapshot_floors(self, rules: list[BuyRule], pools: dict[tuple, list[Listing]],
                               api: dict[tuple, float | None], now: float) -> dict[str, list[tuple[float, float]]]:
        """Write one floor_snapshots row per rule filter key at most once per FLOORS_TTL.
        Returns the last hour of snapshots per key (for the jump downgrade and the crash breaker)."""
        out: dict[str, list[tuple[float, float]]] = {}
        for rule in rules:
            key = rule.filter_key_str()
            if key in out:
                continue
            last = self._snap_written.get(key)
            if last is None:
                snap = await self.storage.latest_snapshot(key)
                last = snap.ts if snap else -1e18
            if now - last >= self.settings.floors_ttl:
                est = estimate_floor(None, pools.get(rule.filter_key(), []), api.get(rule.filter_key()), [], now,
                                     k=self.settings.CLUSTER_K, band=self.settings.CLUSTER_BAND_PCT)
                if est.value is not None:
                    await self.storage.add_snapshot(now, key, est.value, est.n, est.source)
                    self._snap_written[key] = now
            out[key] = [(s.ts, s.floor_ton) for s in await self.storage.snapshots(key, now - HOUR) if s.floor_ton]
        return out

    async def _crash_breaker(self, rules: list[BuyRule], snaps: dict[str, list[tuple[float, float]]],
                             now: float) -> set[int]:
        """Pause a rule whose floor fell more than CRASH_PCT within 1 h. Returns the paused rule ids."""
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from .callbacks import RuleCB, RuleResumeCB

        paused: set[int] = set()
        for rule in rules:
            if rule.id is None:
                continue
            until = await self.storage.get_float(f"rule_paused_until:{rule.id}")
            if until is not None and until > now:
                paused.add(rule.id)
                continue
            drop = floor_crash(snaps.get(rule.filter_key_str(), []), now, self.settings.CRASH_PCT)
            if drop is None:
                continue
            last_alert = await self.storage.get_float(f"crash_alert:{rule.id}")
            if last_alert is not None and now - last_alert < HOUR:
                continue
            minutes = self.settings.CRASH_PAUSE_MIN
            await self.storage.set_setting(f"rule_paused_until:{rule.id}", now + minutes * 60)
            await self.storage.set_setting(f"crash_alert:{rule.id}", now)
            paused.add(rule.id)
            await self._expire_cards("Expired: rule paused by the floor-crash breaker", rule_id=rule.id)
            nonce = await self.issue_rule_resume_nonce(rule.id, now + minutes * 60, now)
            kb = InlineKeyboardBuilder()
            kb.button(text="Resume", callback_data=RuleResumeCB(rule_id=rule.id, nonce=nonce))
            kb.button(text="Keep paused", callback_data=RuleCB(action="keep", rule_id=rule.id))
            await self.notifier.send(f"Rule #{rule.id} paused for {minutes} min: floor −{drop:.0f}% in 1 h",
                                     keyboard=kb.as_markup())
            self._emit_state("pause", now, f"rule #{rule.id} floor crash -{drop:.0f}%")
        return paused

    async def _write_own_snapshot(self, floors: dict[str, float], pools: dict[tuple, list[Listing]],
                                  attrs: dict[str, AttributeFloors | None], now: float) -> None:
        """floors_portals.json for sibling bots: every collection floor (public /collections), plus model
        floors from /collections/filters (collections the rules pin) and the listings seen this cycle."""
        if self.snapshot_path is None or now - self._last_snapshot_file < self.settings.floors_ttl:
            return
        models: dict[str, dict[str, float]] = {}
        for key, af in attrs.items():
            if af is not None:
                models.setdefault(key, {}).update({m: v for m, v in af.models.items() if v > 0})
        for listings in pools.values():
            for x in listings:
                if x.model:
                    table = models.setdefault(short_name(x.collection), {})
                    table[x.model] = min(x.price_ton, table.get(x.model, float("inf")))
        data: dict[str, dict[str, Any]] = {}
        for key, floor in floors.items():
            info = self._meta.get(key)
            name = info.name if info else key
            data[name] = {"floor": floor, "n": (info.listed_count if info else 0) or 0, "models": models.get(key, {})}
        try:
            write_snapshot(self.snapshot_path, build_snapshot(MARKET, self.fees, data, now))
            self._last_snapshot_file = now
        except OSError as exc:
            log.warning("cannot write %s: %s", self.snapshot_path, exc)

    def _estimate(self, listing: Listing, rule: BuyRule, pools: dict[tuple, list[Listing]],
                  api: dict[tuple, float | None], snaps: dict[str, list[tuple[float, float]]], now: float) -> FloorEstimate:
        s = self.settings
        return estimate_floor(
            listing, pools.get(rule.filter_key(), []), api.get(rule.filter_key()), snaps.get(rule.filter_key_str(), []),
            now, k=s.CLUSTER_K, band=s.CLUSTER_BAND_PCT, jump_pct=s.FLOOR_JUMP_PCT,
            min_comparables=s.MIN_COMPARABLES_ATTR if rule.pins_attributes else s.MIN_COMPARABLES_COLLECTION,
        )

    def _deal(self, listing: Listing, est: FloorEstimate, now: float) -> DealMath:
        flags = classify(listing, MARKET, now)
        tg = too_good_flag(listing.price_ton, est.value, self.settings.TOO_GOOD_PCT)
        if tg:
            flags.append(tg)
        return assess(listing, est, self.fees_for(listing.collection), flags)

    async def _evaluate(self, listing: Listing, rules: list[BuyRule], pools: dict[tuple, list[Listing]],
                        api: dict[tuple, float | None], snaps: dict[str, list[tuple[float, float]]],
                        paused_rules: set[int], report: CycleReport, now: float, cycle_mode: str = "dry") -> None:
        rejection: tuple[BuyRule, MatchResult] | None = None
        for rule in rules:
            if norm(rule.collection) != norm(listing.collection):
                continue
            deal = self._deal(listing, self._estimate(listing, rule, pools, api, snaps, now), now)
            result = match(rule, listing, deal)
            if not result.ok:
                if rejection is None or (rejection[1].code == "MISMATCH" and result.code != "MISMATCH"):
                    rejection = (rule, result)
                continue
            if rule.id in paused_rules:
                rejection = (rule, MatchResult(False, "rule paused by the floor-crash breaker", deal.discount_pct,
                                               "RULE_PAUSED", deal))
                continue
            report.matched += 1
            await self._handle_match(rule, listing, deal, report, now)
            return
        if rejection is None:
            return
        rule, result = rejection
        deal = result.deal
        report.rejected += 1
        report.reasons[result.code] += 1
        report.rejections.append(
            f"skip #{listing.number if listing.number is not None else listing.id} {listing.price_ton:.2f} TON: "
            f"{result.reason} (rule #{rule.id})"
        )
        log.log(self._reject_level, "reject %s %.4g TON by rule #%s: %s", listing.title, listing.price_ton, rule.id,
                result.reason)
        near = None
        if deal is not None and result.code in ("OVER_MAX", "LOW_DISCOUNT", "LOW_PROFIT"):
            near = near_miss(rule, listing, deal, pct=self.settings.NEAR_MISS_PCT)
        ev_mode = "shadow" if cycle_mode == "live" and rule.state == "shadow" else cycle_mode
        await self._decide(now, listing, rule, "skip", result.code, deal, result.reason, mode=ev_mode,
                           extra={"near_miss": near.text() if near else None})
        if near is not None and deal is not None:
            await self._near_miss(rule, listing, deal, near, now)

    # ----- decisions and events -----
    def _detail(self, listing: Listing, deal: DealMath | None, text: str, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {"text": text, "title": listing.short_title, "collection": listing.collection,
                             "model": listing.model, "number": listing.number}
        if deal is not None:
            d.update(floor=deal.floor, confidence=deal.confidence, n=deal.n, source=deal.floor_source,
                     cost=round(deal.cost, 6), discount=deal.discount_pct, note=deal.note,
                     profit_ton=None if deal.profit_ton is None else round(deal.profit_ton, 6))
        d.update({k: v for k, v in extra.items() if v is not None})
        return d

    async def _decide(self, now: float, listing: Listing, rule: BuyRule | None, outcome: str, code: str | None,
                      deal: DealMath | None, text: str, *, mode: str = "dry", extra: dict[str, Any] | None = None) -> None:
        await self.storage.add_decision(now, listing.id, listing.price_ton, rule.id if rule else None, outcome, code,
                                        self._detail(listing, deal, text, **(extra or {})))
        kind = {"skip": "skip", "muted": "skip", "near": "skip", "alert": "alert", "dry": "buy", "bought": "buy",
                "failed": "error"}.get(outcome, "skip")
        verdict = {"skip": "SKIP", "muted": "SKIP", "alert": "WATCH", "dry": "BUY", "bought": "BUY"}.get(outcome)
        if code and code.startswith("TRAP"):
            verdict = "AVOID"
        codes = [code] if code else []
        if deal is not None and deal.discount_pct and deal.discount_pct > 0 and outcome in ("alert", "dry", "bought"):
            codes.insert(0, "BELOW_FLOOR")
        self._emit(kind, now, listing, deal, codes, rule, mode, verdict)

    def _emit(self, kind: str, now: float, listing: Listing, deal: DealMath | None, codes: list[str],
              rule: BuyRule | None, mode: str, verdict: str | None) -> None:
        if self.events is None:
            return
        reasons = []
        for c in codes:
            sc = suite_code(c)
            p: dict[str, Any] = {}
            if deal is not None:
                p = {k: round(v, 4) for k, v in (("cost", deal.cost), ("floor", deal.floor),
                                                 ("net", deal.profit_ton)) if v is not None}
            reasons.append({"code": sc, "sev": severity(sc), "p": p})
        money = None
        if deal is not None:
            fees = deal.cost - deal.price + ((deal.floor - deal.net_exit) if deal.floor and deal.net_exit else 0.0)
            usd = None
            rate = self._usd_rate()
            if rate:
                usd = round(deal.cost * rate, 2)
            money = {"ccy": "TON", "price": deal.price, "ref_price": deal.floor, "fees": round(fees, 6),
                     "net_edge": deal.profit_ton, "usd": usd}
        event = {
            "v": 1, "ts": now, "bot": BOT_NAME, "kind": kind, "mode": mode, "id": listing.key,
            "subject": {"type": "gift", "ref": listing.id, "title": listing.short_title, "url": listing.url},
            "money": money, "verdict": verdict, "score": None,
            "confidence": deal.confidence if deal is not None else None, "reasons": reasons,
            "rule_id": rule.id if rule else None, "tx": None,
        }
        try:
            self.events.append(Event.model_validate(event))
        except Exception as exc:  # a bad event must never break the loop
            log.debug("event dropped: %s", exc)

    def _emit_state(self, kind: str, now: float, text: str) -> None:
        if self.events is None:
            return
        try:
            self.events.append(Event.model_validate({
                "v": 1, "ts": now, "bot": BOT_NAME, "kind": kind, "mode": "dry", "id": f"{kind}:{now:.0f}",
                "subject": {"type": "market", "ref": MARKET, "title": text}, "reasons": [],
            }))
        except Exception as exc:
            log.debug("event dropped: %s", exc)

    # ----- matches -----
    async def _peer_view(self, listing: Listing, rule: BuyRule, deal: DealMath) -> PeerView | None:
        if self.peers is None or not self.peers.enabled:
            return None
        timeout = None
        if self._peers_deadline is not None:  # inside run_cycle: all matches share one PEERS_TIMEOUT_SEC
            timeout = self._peers_deadline - time.monotonic()
        view = await self.peers.view(listing, rule, deal, timeout=timeout)
        return None if view.empty else view

    def _refresh_rate(self) -> None:
        """Refresh the TON->USD rate in the background (once per TTL); a buy never waits for it."""
        rate = self.peers.rate if self.peers is not None else None
        if rate is None or rate.fixed is not None or (self._rate_task is not None and not self._rate_task.done()):
            return
        self._rate_task = asyncio.create_task(rate.usd_per_ton(), name="ton-rate")

    async def _prime_rate(self) -> None:
        """At startup (no buy is waiting yet): fetch the TON->USD rate once, bounded by PEERS_TIMEOUT_SEC, so the
        first cycle's Telegram-value guard can convert USD to TON. After that, cycles use the cached rate."""
        rate = self.peers.rate if self.peers is not None else None
        if rate is None or rate.fixed is not None or rate.value is not None:
            return
        try:
            await asyncio.wait_for(rate.usd_per_ton(), self.settings.PEERS_TIMEOUT_SEC)
        except Exception as exc:  # timeout or network trouble: no rate yet, the guard just stays quiet
            log.debug("TON rate not primed: %s", exc)

    def _usd_rate(self) -> float | None:
        if self.peers is None or self.peers.rate is None or self.settings.DISPLAY_CURRENCY.lower() != "usd":
            return None
        return self.peers.rate.value  # cached; run_cycle refreshes it in the background

    async def _handle_match(self, rule: BuyRule, listing: Listing, deal: DealMath, report: CycleReport, now: float) -> None:
        s = self.settings
        limits = await self.effective_limits()
        downgrades: list[tuple[str, str]] = []
        too_good = deal.flag("TOO_GOOD")
        if too_good:
            downgrades.append(("TOO_GOOD", too_good.text))
        if deal.lock_days:
            downgrades.append(("LOCKED_ALLOWED", f"locked for {deal.lock_days:.0f} d"))
        peers = await self._peer_view(listing, rule, deal)
        downgrades += guards(peers, deal, cheaper_pct=s.CHEAPER_ELSEWHERE_PCT, tg_guard=s.TG_VALUE_GUARD)
        confirm_above = s.confirm_above(limits.max_price_ton)  # follows a /preset that lowered the max
        if confirm_above < deal.cost <= limits.max_price_ton + 1e-9:
            # (above MAX_PRICE_TON the limit check refuses it anyway, with its own reason)
            downgrades.append(("CONFIRM_ABOVE", f"buys above {confirm_above:g} TON always ask first"))
        exit_best = None
        if rule.exit == "best" and peers is not None:
            exit_best = best_exit(MARKET, deal.net_exit, peers.quotes, deal.fees.withdraw_ton)
        for code, _ in downgrades:
            report.reasons[code] += 1
        mode = stricter_mode(rule.mode, "confirm" if downgrades else "auto")
        dry_global = await self.is_dry(now)
        shadow = rule.state == "shadow" and not dry_global
        practice = dry_global or shadow
        ev_mode = "dry" if dry_global else ("shadow" if shadow else "live")
        spent = await self.storage.spent_since(now - DAY, dry=practice)
        buys = await self.storage.buys_since(now - HOUR, dry=practice)
        view = DealView(
            listing=listing, deal=deal, rule=rule, mode=mode, status="", practice=practice,
            asking_first=[t for _, t in downgrades], peers=peers, best_exit=exit_best, usd_rate=self._usd_rate(),
            budget_left=max(0.0, limits.daily_budget_ton - spent), buys_left=max(0, limits.max_buys_per_hour - buys),
            token=secrets.token_urlsafe(6), market=MARKET, market_bot=s.PORTALS_BOT_USERNAME, market_title=MARKET_TITLE,
            fees_verified=s.FEES_VERIFIED, llm=llm_available(s), now=now, shadow=shadow,
        )

        if mode != "auto":
            muted = await self.storage.muted_key(mute_keys(listing.collection, listing.model), now)
            if muted:
                report.reasons["MUTED"] += 1
                await self._decide(now, listing, rule, "muted", "MUTED", deal, f"muted ({muted})", mode=ev_mode)
                return
            view.pending = mode == "confirm"
            view.status = (f"Waiting for your tap ({s.CONFIRM_TTL_SEC} s)" if mode == "confirm"
                           else "Alert only (watch mode): no Buy button")
            await self._send_card(view, status="pending" if mode == "confirm" else "watch", now=now,
                                  ttl=s.CONFIRM_TTL_SEC if mode == "confirm" else DAY)
            report.alerts += 1
            await self._decide(now, listing, rule, "alert", downgrades[0][0] if downgrades else "OK", deal,
                               "; ".join(t for _, t in downgrades) or f"{mode} mode: card sent", mode=ev_mode,
                               extra={"mode": mode})
            return

        # auto mode: one locked step re-reads the switches, checks the limits on all-in cost against fresh
        # totals and (practice) buys; then one card with the outcome
        spend = await self._spend(rule, listing, deal, now, practice=practice, detail="SHADOW_RULE" if shadow else "")
        if spend.practice and not practice:  # /dry on or /panic landed while we were looking: practice only
            practice, shadow, ev_mode = True, False, "dry"
            view.practice, view.shadow = True, False
        if spend.kind == "refused":  # a switch, breaker or the balance said no; whoever refused has notified
            report.skipped += 1
            report.reasons[spend.code] += 1
            await self._decide(now, listing, rule, "skip", spend.code, deal, f"not bought: {spend.text}", mode=ev_mode)
            return
        if spend.kind == "blocked":
            report.skipped += 1
            report.reasons[spend.code] += 1
            view.status = f"Not bought: {spend.text}"
            await self.storage.record_purchase(listing, dry=practice, status="skipped", floor=deal.floor,
                                               rule_id=rule.id, error=spend.text, cost_ton=deal.cost, created_at=now)
            await self._send_card(view, status="skipped", now=now, ttl=DAY)
            await self.notifier.send(format_skip(listing, spend.text), silent=True)
            await self._decide(now, listing, rule, "skip", spend.code, deal, spend.text, mode=ev_mode)
            return
        if spend.kind == "practice":
            report.dry_bought += 1
            view.status = "PRACTICE: would buy" + (" (shadow rule)" if shadow else "")
            await self._send_card(view, status="dry", now=now, ttl=DAY)
            await self._decide(now, listing, rule, "dry", "SHADOW_RULE" if shadow else "DRY_RUN", deal,
                               f"practice buy for {deal.cost:.2f} TON all-in", mode=ev_mode)
            return
        if spend.kind == "bought":
            report.bought += 1
            view.status = f"BOUGHT in {spend.elapsed:.1f} s"
            await self._send_card(view, status="bought", now=now, ttl=DAY)
            await self.notifier.send(format_buy_ok(listing, spend.text))
            await self._decide(now, listing, rule, "bought", "OK", deal, f"bought for {deal.cost:.2f} TON", mode="live")
            return
        report.failed += 1
        view.status = f"Buy failed: {spend.text}"
        await self._send_card(view, status="skipped", now=now, ttl=DAY)
        await self.notifier.send(format_buy_failed(listing, spend.text))
        await self._decide(now, listing, rule, "failed", "BUY_FAILED", deal, spend.text, mode="live")

    # ----- the one spend path (auto-buys and taps) -----
    async def _spend(self, rule: BuyRule, listing: Listing, deal: DealMath, now: float, *, practice: bool,
                     detail: str, token: str | None = None) -> Spend:
        """Commit one buy, practice or live. Everything from the switch re-check to the purchase row runs
        under one lock, so concurrent taps and the loop see each other's spend and the daily budget and the
        hourly cap hold. A caller's `practice` can only be tightened here, never relaxed."""
        async with self._spend_lock:
            if self._closing:
                return Spend("refused", "PAUSED", "the bot is shutting down", practice=practice)
            practice = practice or rule.state == "shadow" or await self.is_dry()
            refused = await self._spend_gate(rule.id, live=not practice, token=token, listing=listing, deal=deal)
            if refused:
                return Spend("refused", *refused, practice=practice)
            limits = await self.effective_limits()
            blocked = check_limits_detail(deal.cost, limits,
                                          spent_24h=await self.storage.spent_since(now - DAY, dry=practice),
                                          buys_last_hour=await self.storage.buys_since(now - HOUR, dry=practice))
            if blocked:
                return Spend("blocked", *blocked, practice=practice)
            if practice:
                await self.storage.record_purchase(listing, dry=True, status="dry", floor=deal.floor, rule_id=rule.id,
                                                   error=detail or None, cost_ton=deal.cost, created_at=now)
                return Spend("practice", practice=True)
            return await self._live_buy(rule, listing, deal, now, token)

    async def _rule_hold(self, rule_id: int | None, now: float | None = None, *, live: bool = False) -> tuple[str, str] | None:
        """(code, text) when this rule may not buy now: removed, disabled, paused, crash-paused (or shadow, for live)."""
        if rule_id is None:
            return None
        now = self.clock() if now is None else now
        rule = await self.storage.get_rule(rule_id)
        if rule is None or not rule.enabled:
            return "DISABLED", f"rule #{rule_id} was removed or disabled"
        if rule.state == "paused":
            return "RULE_PAUSED", f"rule #{rule_id} is paused"
        until = await self.storage.get_float(f"rule_paused_until:{rule_id}")
        if until is not None and until > now:
            return "RULE_PAUSED", f"rule #{rule_id} is paused by the floor-crash breaker"
        if live and rule.state == "shadow":
            return "SHADOW_RULE", f"rule #{rule_id} is in shadow (practice only)"
        return None

    async def _spend_gate(self, rule_id: int | None, *, live: bool, token: str | None = None,
                          listing: Listing | None = None, deal: DealMath | None = None) -> tuple[str, str] | None:
        """Every switch re-read from storage right before money moves: (code, text) when the buy must not happen."""
        if await self.is_panic():
            return "PAUSED", "stopped by /panic"
        if await self.is_paused():
            return "PAUSED", "the bot is paused"
        hold = await self._rule_hold(rule_id, live=live)
        if hold:
            return hold
        if rule_id is not None and listing is not None and deal is not None:
            changed = await self._rule_changed(rule_id, listing, deal, auto=token is None)
            if changed:
                return changed
        if token is not None:
            card = await self.storage.get_pending(token)
            if card is None or card.status != "executing":
                return "PAUSED", "the card was cancelled"
        if live and await self.is_dry():
            return "DRY_RUN", "practice mode is on"
        return None

    async def _rule_changed(self, rule_id: int, listing: Listing, deal: DealMath, *, auto: bool) -> tuple[str, str] | None:
        """The rule as stored now (an edit may have landed since the cycle read it): its max, filters and,
        for loop buys, its auto mode must still allow this buy."""
        fresh = await self.storage.get_rule(rule_id)
        if fresh is None:
            return "DISABLED", f"rule #{rule_id} was removed or disabled"
        if auto and fresh.mode != "auto":
            return "MODE_CHANGED", f"rule #{rule_id} was switched to {fresh.mode} mode"
        result = match(fresh, listing, deal)
        if not result.ok:
            return result.code, f"rule #{rule_id} changed: {result.reason}"
        return None

    async def _live_buy(self, rule: BuyRule, listing: Listing, deal: DealMath, now: float, token: str | None) -> Spend:
        """Real money, called under the spend lock: balance (and the balance-mismatch breaker), every switch
        re-read once more (a /panic, /dry on, /pause or breaker may have landed during the balance call),
        then buy() and the purchase row. The Portals buy payload carries the listing's price string, so the
        server rejects a changed price. The purchase row is written as 'unknown' (it counts toward the limits)
        before the order is sent and settled to ok/error afterwards, so a buy cut off by a shutdown or a crash
        mid-request still counts after a restart. Only an explicit success settles it as 'ok': any other
        answer (an odd 2xx body, a timeout, a 5xx) stays 'unknown' and keeps counting; a 4xx is 'error'."""
        try:
            balance = await self.client.balance()
        except (PortalsError, TgAuthError, ConfigError) as exc:
            await self.notifier.send(format_skip(listing, f"cannot read the balance ({exc})"))
            return Spend("refused", "LIMIT_BALANCE", f"cannot read the balance ({exc})")
        if await self._balance_guard(balance, now):
            return Spend("refused", "PAUSED", "balance mismatch, paused")
        if balance < deal.cost:
            text = f"balance {balance:.2f} TON < cost {deal.cost:.2f} TON"
            await self.notifier.send(format_skip(listing, text))
            return Spend("refused", "LIMIT_BALANCE", text)
        refused = await self._spend_gate(rule.id, live=True, token=token, listing=listing, deal=deal)
        if refused:
            return Spend("refused", *refused)
        row = await self.storage.record_purchase(
            listing, dry=False, status="unknown", floor=deal.floor, rule_id=rule.id, cost_ton=deal.cost, created_at=now,
            error="interrupted before Portals answered (result unknown: counted toward your limits, check Portals)")
        started = time.perf_counter()
        try:
            ok, detail = classify_buy_response(await self.client.buy(listing))
            unknown = not ok  # a 2xx that is not an explicit success may still be a purchase
        except Exception as exc:  # PortalsError, or anything unexpected: the order may or may not have gone through
            ok = False
            unknown = buy_outcome_unknown(exc)
            detail = (f"{exc} (result unknown: counted toward your limits, check Portals)" if unknown else str(exc))[:500]
        elapsed = time.perf_counter() - started
        status = "ok" if ok else ("unknown" if unknown else "error")
        await self.storage.settle_purchase(row, status=status, error=None if ok else detail)
        await self._after_buy(ok, detail, now)
        return Spend("bought" if ok else status, "OK" if ok else "BUY_FAILED", detail, elapsed=elapsed)

    async def _after_buy(self, ok: bool, detail: str, now: float) -> None:
        if ok:
            self.consecutive_buy_errors = 0
            return
        self.consecutive_buy_errors += 1
        if self.consecutive_buy_errors >= self.settings.AUTO_PAUSE_ERRORS:
            await self.set_paused(True)
            await self._expire_cards("Expired: paused after failed buys")
            await self.notifier.send(
                f"Paused: {self.consecutive_buy_errors} buys failed in a row (last: {detail}). /resume to continue."
            )
            self._emit_state("pause", now, "executor errors")

    async def _expire_cards(self, line: str, *, rule_id: int | None = None) -> int:
        """A breaker fired: pending Buy buttons (all, or this rule's) stop working at once."""
        cards = await self.storage.expire_pending(None, rule_id=rule_id)
        for card in cards:
            await self._edit_card(card, line)
        return len(cards)

    async def _balance_guard(self, balance: float, now: float) -> bool:
        """True (and paused) when the balance fell by more than our own recorded live spend + tolerance.
        Portals may hold funds as frozen (offers): available + frozen is compared when the API sends it."""
        if not self.settings.BALANCE_GUARD:
            return False
        frozen = getattr(self.client, "last_frozen", None) or 0.0
        total = balance + frozen
        prev = await self.storage.get_float("balance_last")
        prev_ts = await self.storage.get_float("balance_ts")
        spent = await self.storage.spent_since(prev_ts, dry=False) if prev_ts is not None else 0.0
        await self.storage.set_setting("balance_last", total)
        await self.storage.set_setting("balance_ts", now)
        hit = balance_mismatch(prev, total, spent, self.settings.BALANCE_TOLERANCE_TON)
        if hit is None:
            return False
        fell, spent = hit
        await self.set_paused(True)
        await self._expire_cards("Expired: paused, the balance does not add up")
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from .callbacks import GateCB, ResumeCB

        kb = InlineKeyboardBuilder()
        kb.button(text="Resume", callback_data=ResumeCB(nonce=await self.issue_resume_nonce(now)))
        kb.button(text="Keep paused", callback_data=GateCB(action="keep"))
        live = ("" if await self.is_dry(now) else
                " LIVE is still armed: Resume continues real buys; /dry on first to resume in practice.")
        await self.notifier.send(
            f"Balance fell {fell:.2f} TON but I spent {spent:.2f} TON: someone else may be using this account. "
            f"Paused.{live}",
            keyboard=kb.as_markup(),
        )
        self._emit_state("pause", now, "balance mismatch")
        return True

    # ----- cards -----
    async def _send_card(self, view: DealView, *, status: str, now: float, ttl: float) -> str:
        """Render, send and store a deal card (pending row) so its buttons and edits work later."""
        assert view.token is not None
        card = render_deal_card(view)
        tpl = render_deal_card(view, with_slot=True)
        final_view = replace(view, pending=False)
        final_kb = render_deal_card(final_view).keyboard
        listing = view.listing
        payload = {
            "listing": {"id": listing.id, "collection": listing.collection, "model": listing.model,
                        "backdrop": listing.backdrop, "symbol": listing.symbol, "number": listing.number,
                        "price_ton": listing.price_ton, "raw": listing.raw},
            "deal": view.deal.to_dict(),
            "title": listing.short_title,
            "mute_key": mute_keys(listing.collection, listing.model)[-1],
            "html_tpl": tpl.html,
            "kb_final": final_kb.model_dump_json(exclude_none=True) if final_kb else None,
            "mode": view.mode,
            "practice": view.practice,
        }
        await self.storage.create_pending(view.token, listing.id, view.rule.id, listing.price_ton, view.deal.cost,
                                          payload, now, ttl, status=status)
        msgs = await self.notifier.send_card(card)
        if msgs:
            await self.storage.set_pending_msgs(view.token, msgs)
        return view.token

    async def _edit_card(self, card: PendingCard, status_line: str) -> None:
        from aiogram.types import InlineKeyboardMarkup

        tpl = card.payload.get("html_tpl")
        if not tpl:
            return
        kb_json = card.payload.get("kb_final")
        kb = InlineKeyboardMarkup.model_validate_json(kb_json) if kb_json else None
        await self.notifier.edit(card.msgs, fill_status(tpl, status_line, is_html=True), html=True, keyboard=kb,
                                 note=f"{card.payload.get('title', card.listing_id)} → {status_line}")

    async def _near_miss(self, rule: BuyRule, listing: Listing, deal: DealMath, near: Any, now: float) -> None:
        s = self.settings
        while self._near_times and now - self._near_times[0] >= HOUR:
            self._near_times.popleft()
        if len(self._near_times) >= s.NEAR_MISS_MAX_PER_HOUR:
            return
        ignored = await self.storage.get_float(f"near_ignore:{rule.id}")
        if ignored is not None and ignored > now:
            return
        if await self.storage.muted_key(mute_keys(listing.collection, listing.model), now):
            return
        self._near_times.append(now)
        token = secrets.token_urlsafe(6)
        limits = await self.effective_limits()
        # A shadow rule only ever practises, so its near miss is PRACTICE even inside a LIVE window.
        practice = await self.is_dry(now) or rule.state == "shadow"
        card = render_near_miss(listing, deal, rule, near, token, max_cap=limits.max_price_ton, practice=practice,
                                shadow=rule.state == "shadow" and not await self.is_dry(now),
                                fees_verified=s.FEES_VERIFIED, llm=llm_available(s))
        await self.storage.create_pending(
            token, listing.id, rule.id, listing.price_ton, deal.cost,
            {"kind": "near", "title": listing.short_title, "check": near.check, "new_max": near.new_max,
             "deal": deal.to_dict()}, now, DAY,
            status="near",
        )
        msgs = await self.notifier.send(card.html, html=True, keyboard=card.keyboard, silent=True, plain=card.plain)
        if msgs:
            await self.storage.set_pending_msgs(token, msgs)

    # ----- taps (inline buttons). The aiogram handlers and the mock FakeTapper both call these. -----
    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    async def tap(self, action: str, token: str, user_id: int | None, now: float | None = None) -> TapResult:
        now = self.clock() if now is None else now
        if not self.is_admin(user_id):
            return TapResult(False, "Not allowed")
        if action == "b":
            return await self._tap_buy(token, now)
        card = await self.storage.get_pending(token)
        if card is None:
            return TapResult(False, "Expired or already handled")
        title = card.payload.get("title", card.listing_id)
        if action == "s":
            if await self.storage.set_pending_status(token, "skipped", only_from=("pending",)):
                await self._edit_card(card, "Skipped")
                await self._tap_decision(card, now, "SKIPPED", "skipped by you")
                return TapResult(True, f"Skipped {title}", "skipped")
            return TapResult(False, "Expired or already handled")
        if action == "m":
            key = card.payload.get("mute_key") or norm(title)
            await self.storage.add_mute(key, now + MUTE_SEC)
            if await self.storage.set_pending_status(token, "skipped", only_from=("pending",)):
                await self._edit_card(card, "Skipped · muted for 24 h")
            return TapResult(True, f"Muted {key} for 24 h. Decisions are still logged (/why).", card.status)
        if action == "w":
            text = why_text(await self.storage.decisions_for(card.listing_id), title=title,
                            tz_offset_hours=self.settings.TZ_OFFSET_HOURS)
            return TapResult(True, "Why?", card.status, reply=text)
        if action == "x":
            decisions = await self.storage.decisions_for(card.listing_id)
            event = {"title": title, "deal": card.payload.get("deal"),
                     "decisions": [{"outcome": d.outcome, "reason": d.reason_code, "detail": d.detail} for d in decisions]}
            text = await explain_more(event, self.settings)
            return TapResult(text is not None, "Explain more", card.status, reply=text or "No explanation available.")
        return TapResult(False, "Unknown action")

    async def _tap_decision(self, card: PendingCard, now: float, code: str, text: str) -> None:
        await self.storage.add_decision(now, card.listing_id, card.price_ton, card.rule_id, "tap", code,
                                        {"text": text, "title": card.payload.get("title")})

    async def _finish(self, card: PendingCard, status: str, line: str, now: float, code: str, text: str) -> TapResult:
        await self.storage.set_pending_status(card.token, status)
        await self._edit_card(card, line)
        await self._tap_decision(card, now, code, text)
        return TapResult(status in ("dry", "bought"), text, status)

    async def _tap_buy(self, token: str, now: float) -> TapResult:
        if await self.is_panic():
            return TapResult(False, "Stopped by /panic: /resume first, then /dry off")
        if await self.is_paused():
            return TapResult(False, "Paused: nothing is bought until /resume")
        pending = await self.storage.get_pending(token)
        if pending is not None and pending.status == "pending":
            hold = await self._rule_hold(pending.rule_id, now)
            if hold:
                return TapResult(False, f"Not bought: {hold[1]}")
        if not await self.storage.claim_pending(token, now):
            return TapResult(False, "Expired or already handled")
        card = await self.storage.get_pending(token)
        assert card is not None
        # The card keeps its own mode: one sent as PRACTICE (or without the flag) only ever buys in practice,
        # even if LIVE was armed since (arm_live expires such cards; this covers one that slipped through, or
        # a shadow rule promoted after its card went out). A LIVE card still turns practice if LIVE ended.
        card_practice = card.payload.get("practice") is not False
        p = card.payload["listing"]
        hint = Listing(p["id"], p["collection"], p["model"], p["backdrop"], p["symbol"], p["number"], p["price_ton"],
                       raw=p.get("raw") or {})
        try:
            fresh = await self.client.get_listing(card.listing_id, hint)
        except Exception as exc:  # network error: never buy blind
            log.warning("re-check failed for %s: %s", card.listing_id, exc)
            if card.expires_at > now:
                await self.storage.set_pending_status(token, "pending", only_from=("executing",))
            await self._edit_card(card, "Couldn't re-check, not bought")
            return TapResult(False, "couldn't re-check, not bought", "pending")
        if fresh is None:
            return await self._finish(card, "gone", "Gone: sold to someone else", now, "GONE", "gone: sold to someone else")
        if abs(fresh.price_ton - card.price_ton) > 1e-9:
            return await self._finish(card, "changed", "Price changed, not bought", now, "PRICE_CHANGED",
                                      f"price changed {card.price_ton:.2f} → {fresh.price_ton:.2f}, not bought")
        rule = await self.storage.get_rule(card.rule_id) if card.rule_id is not None else None
        if rule is None or not rule.enabled:
            return await self._finish(card, "skipped", "Rule removed, not bought", now, "DISABLED", "rule removed, not bought")
        old = DealMath.from_dict(card.payload["deal"])
        est = FloorEstimate(old.floor, old.floor_source, old.n, old.confidence, old.note)
        deal = self._deal(fresh, est, now)
        result = match(rule, fresh, deal)
        if not result.ok:
            return await self._finish(card, "skipped", f"Re-check failed: {result.reason}", now, result.code,
                                      f"re-check failed: {result.reason}")
        spend = await self._spend(rule, fresh, deal, now, practice=card_practice or await self.is_dry(now),
                                  detail="tap", token=token)
        if spend.kind in ("refused", "blocked"):
            return await self._finish(card, "skipped", f"Not bought: {spend.text}", now, spend.code,
                                      f"not bought: {spend.text}")
        if spend.kind == "practice":
            shadow = rule.state == "shadow" and not await self.is_dry(now)
            text = f"re-check ok → PRACTICE: bought {fresh.short_title} for {deal.cost:.2f} TON"
            await self._decide(now, fresh, rule, "dry", "SHADOW_RULE" if shadow else "DRY_RUN", deal,
                               "practice buy after your tap", mode="shadow" if shadow else "dry")
            return await self._finish(card, "dry", f"PRACTICE: bought for {deal.cost:.2f} TON", now, "OK", text)
        if spend.kind == "bought":
            await self._decide(now, fresh, rule, "bought", "OK", deal, "bought after your tap", mode="live")
            return await self._finish(card, "bought", f"BOUGHT in {spend.elapsed:.1f} s", now, "OK",
                                      f"re-check ok → BOUGHT {fresh.short_title} for {deal.cost:.2f} TON")
        return await self._finish(card, "skipped", f"Buy failed: {spend.text}", now, "BUY_FAILED",
                                  f"buy failed: {spend.text}")

    async def drain(self, timeout: float) -> bool:
        """Shutdown: no new spend starts; wait (bounded) until the one in flight is recorded. True when idle."""
        self._closing = True
        try:
            async with asyncio.timeout(timeout):
                async with self._spend_lock:
                    return True
        except TimeoutError:
            return False

    async def near_action(self, action: str, token: str, user_id: int | None, now: float | None = None) -> TapResult:
        """Near-miss buttons: r = raise the rule max (never above MAX_PRICE_TON), i = ignore this rule today."""
        now = self.clock() if now is None else now
        if not self.is_admin(user_id):
            return TapResult(False, "Not allowed")
        card = await self.storage.get_pending(token)
        if card is None or card.status != "near" or card.rule_id is None or card.expires_at <= now:
            return TapResult(False, "Expired or already handled")
        if action == "r":
            new_max = card.payload.get("new_max")
            cap = (await self.effective_limits()).max_price_ton
            if new_max is None or new_max > cap + 1e-9:
                return TapResult(False, f"Cannot raise above MAX_PRICE_TON {cap:g}")
            ok = await self.storage.update_rule(card.rule_id, max_price_ton=float(new_max))
            await self.storage.set_pending_status(token, "skipped")
            return TapResult(ok, f"Rule #{card.rule_id}: max raised to {new_max:.2f} TON" if ok else "Rule not found")
        if action == "i":
            end_of_day = now - (now + self.settings.TZ_OFFSET_HOURS * HOUR) % DAY + DAY
            await self.storage.set_setting(f"near_ignore:{card.rule_id}", end_of_day)
            await self.storage.set_pending_status(token, "skipped")
            return TapResult(True, f"No more near-miss nudges for rule #{card.rule_id} today")
        return TapResult(False, "Unknown action")

    async def promote(self, rule_id: int, now: float | None = None) -> str:
        """The only path from shadow to live for a rule."""
        now = self.clock() if now is None else now
        rule = await self.storage.get_rule(rule_id)
        if rule is None:
            return f"Rule #{rule_id} not found."
        await self.storage.update_rule(rule_id, state="live", promoted_at=now)
        note = " Live buying also needs DRY_RUN=false in .env and /dry off." if self.live_locked else ""
        return f"Rule #{rule_id} is live (no longer shadow).{note}"

    # ----- housekeeping -----
    async def tick(self, now: float | None = None, *, marks_search: bool = True) -> None:
        """Between cycles: expire cards, end an expired live gate, mark positions, run breakers, digest."""
        now = self.clock() if now is None else now
        self.last_tick_at = now
        for card in await self.storage.expire_pending(now):
            await self._edit_card(card, "Expired")
        await self.is_dry(now)
        if now - self.last_mark_at >= self.settings.MARK_INTERVAL_SEC:
            await self.run_marks(now, search=marks_search)
        await self._losing_streak(now)
        await self._maybe_digest(now)
        await self._maybe_hindsight(now)

    async def run_marks(self, now: float | None = None, *, search: bool = True) -> int:
        from .ledger import mark_positions

        now = self.clock() if now is None else now
        self.last_mark_at = now

        async def do_search(collection: str, model: str | None) -> list[Listing]:
            return await self.client.search_listings(SearchFilters(
                collections=[collection], models=[model] if model else [], sort=SORT_PRICE_ASC,
                limit=self.settings.SEARCH_LIMIT))

        return await mark_positions(self.storage, self.settings, now, self.fees_for, do_search if search else None)

    async def _losing_streak(self, now: float) -> None:
        n = self.settings.LOSS_STREAK_N
        recent = await self.storage.last_live_positions(n)
        if not losing_streak(recent, n, self.settings.LOSS_STREAK_PCT):
            return
        newest = str(recent[0].id)
        if await self.storage.get_setting("streak_flagged") == newest:
            return
        await self.storage.set_setting("streak_flagged", newest)
        await self.storage.set_flag("dry", True)
        await self.storage.delete_setting("live_until")
        await self.notifier.send(
            f"{n} losing live buys in a row (each marked more than {self.settings.LOSS_STREAK_PCT:g}% below cost): "
            "back to practice. /pnl for details, /dry off to go live again."
        )
        self._emit_state("pause", now, "losing streak")

    async def _maybe_digest(self, now: float) -> None:
        hour = self.settings.DIGEST_HOUR
        if hour < 0:
            return
        local = now + self.settings.TZ_OFFSET_HOURS * HOUR
        if int(local % DAY // HOUR) != hour:
            return
        day = int(local // DAY)
        if await self.storage.get_setting("digest_day") == str(day):
            return
        await self.storage.set_setting("digest_day", day)
        from .digest import build_digest

        start = (day - 1) * DAY - self.settings.TZ_OFFSET_HOURS * HOUR
        text = await build_digest(self, start, start + DAY)
        await self.notifier.send(text, silent=True)

    async def _maybe_hindsight(self, now: float) -> None:
        """Shadow rules: after SHADOW_MIN_HOURS with SHADOW_MIN_MATCHES (or SHADOW_MAX_HOURS), send the hindsight card."""
        for rule in await self.storage.list_rules(only_enabled=True):
            if rule.state != "shadow" or rule.id is None:
                continue
            card = await self.hindsight_card(rule, now)
            if card is None:
                continue
            text, kb = card
            await self.storage.set_setting(f"shadow_due:{rule.id}", now + DAY)
            await self.notifier.send(text, keyboard=kb)

    async def hindsight_card(self, rule: BuyRule, now: float, *, force: bool = False):
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from .callbacks import ShadowCB
        from .ledger import hindsight_text, races_lost, summarize

        s = self.settings
        started = (await self.storage.get_float(f"shadow_since:{rule.id}")
                   or await self.storage.rule_created_at(rule.id) or self.clock())
        hours = (now - started) / HOUR
        due = await self.storage.get_float(f"shadow_due:{rule.id}")
        positions = [p for p in await self.storage.positions(since=started, dry=True) if p.rule_id == rule.id]
        if not force:
            if due is not None and now < due:
                return None
            ready = hours >= s.SHADOW_MIN_HOURS and len(positions) >= s.SHADOW_MIN_MATCHES
            if not (ready or hours >= s.SHADOW_MAX_HOURS):
                return None
        stats = summarize(positions, practice=True, days=hours / 24 + 1, now=now, fees=self.fees).get("total")
        sniped, _ = await races_lost(self.storage, positions, s.SNIPED_WINDOW_SEC, now)
        text = hindsight_text(rule, hours, stats, sniped, self.live_locked)
        kb = InlineKeyboardBuilder()
        kb.button(text="Go live", callback_data=ShadowCB(action="live", rule_id=rule.id))
        kb.button(text="Keep testing 24h", callback_data=ShadowCB(action="more", rule_id=rule.id))
        kb.button(text="Delete", callback_data=ShadowCB(action="del", rule_id=rule.id))
        return text, kb.as_markup()

    # ----- loop -----
    async def run_forever(self, stop: asyncio.Event, max_cycles: int | None = None) -> None:
        s = self.settings
        await self.storage.prune_seen(s.SEEN_TTL_DAYS)
        await self.storage.prune(self.clock(), obs_days=s.OBS_TTL_DAYS, snap_days=s.SNAPSHOT_TTL_DAYS,
                                 decision_days=s.DECISIONS_TTL_DAYS)
        log.info("engine started: poll=%ss practice=%s", s.POLL_INTERVAL, await self.is_dry())
        await self._prime_rate()
        while not stop.is_set():
            delay = s.POLL_INTERVAL
            cycle_ok = False
            try:
                report = await self.run_cycle()
                if report.paused:
                    delay = max(s.POLL_INTERVAL, 5.0)
                elif report.new or report.matched:
                    log.info("cycle %d: %s", self.stats.cycles, report.summary())
                if self.stats.consecutive_errors >= ERROR_ALERT_AFTER:
                    await self.notifier.send("API recovered, polling resumed.")
                self.stats.consecutive_errors = 0
                self.stats.last_error = None
                self._stale_warned = False
                cycle_ok = True
            except RateLimited as exc:
                await self._on_error(exc)
                delay = max(exc.retry_after, backoff_delay(self.stats.consecutive_errors, s.POLL_INTERVAL, s.MAX_BACKOFF))
            except (PortalsError, TgAuthError, ConfigError) as exc:
                await self._on_error(exc)
                delay = backoff_delay(self.stats.consecutive_errors, s.POLL_INTERVAL, s.MAX_BACKOFF)
            except Exception as exc:  # keep sniping on unexpected bugs, but loudly
                log.exception("cycle crashed")
                await self._on_error(exc)
                delay = backoff_delay(self.stats.consecutive_errors, s.POLL_INTERVAL, s.MAX_BACKOFF)
            # Housekeeping runs after every cycle, good or failed: cards expire, an expired live gate ends and
            # the breakers and digest keep running while the API is down. After a failed cycle it reads stored
            # data only (no mark searches), so it adds no requests to a struggling API.
            try:
                await self.tick(marks_search=cycle_ok)
            except Exception as exc:
                log.warning("housekeeping failed: %s", exc)
            self.stats.current_delay = delay
            if max_cycles is not None and self.stats.cycles >= max_cycles:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay * random.uniform(0.85, 1.15))
            except TimeoutError:
                pass
        log.info("engine stopped")

    async def _on_error(self, exc: BaseException) -> None:
        self.stats.consecutive_errors += 1
        self.stats.api_errors += 1
        self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
        day = int((self.clock() + self.settings.TZ_OFFSET_HOURS * HOUR) // DAY)
        try:
            count = int(await self.storage.get_setting(f"errors:{day}", "0") or 0)
            await self.storage.set_setting(f"errors:{day}", count + 1)
        except Exception:  # storage trouble must not hide the original error
            pass
        log.warning("cycle failed (%d in a row): %s", self.stats.consecutive_errors, self.stats.last_error)
        if self.stats.consecutive_errors == ERROR_ALERT_AFTER:
            await self.notifier.send(
                f"Engine: {self.stats.consecutive_errors} failed cycles in a row. Last error: {self.stats.last_error}"
            )
        now = self.clock()
        last_ok = self.stats.last_ok_at
        if last_ok is not None and now - last_ok > self.settings.STALE_DATA_SEC and not self._stale_warned:
            # No fresh data means no new buys (every buy needs a fresh cycle or a re-check of the listing).
            self._stale_warned = True
            await self.notifier.send(f"Market data is stale ({now - last_ok:.0f}s without a good cycle): "
                                     "no buys until it recovers. /doctor may help.")
            self._emit_state("pause", now, "stale data")
