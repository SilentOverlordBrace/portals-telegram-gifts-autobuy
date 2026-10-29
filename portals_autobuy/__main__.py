"""CLI: python -m portals_autobuy {login,run,once,mock,floors,doctor}."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError

from . import updater as _updater
from .config import ConfigError, Settings, get_settings
from .tg_auth import TgAuthError, mask

log = logging.getLogger("portals_autobuy")

PKG_DIR = Path(__file__).resolve().parent
FIXTURES = PKG_DIR.parent / "tests" / "fixtures"
MOCK_ADMIN_ID = 1
MARKET = "portals"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("telethon", "aiogram.event", "httpx", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _make_bot(settings: Settings):
    if not settings.BOT_TOKEN:
        return None
    from aiogram.utils.token import TokenValidationError

    from .bot import create_bot

    try:
        return create_bot(settings)
    except TokenValidationError as exc:
        raise ConfigError("BOT_TOKEN in .env is not a valid Telegram bot token") from exc


async def _close_bot(bot) -> None:
    if bot is not None:
        with contextlib.suppress(Exception):
            await bot.session.close()


async def open_storage(settings: Settings, path: str | Path | None = None):
    from .storage import Storage

    storage = await Storage(path or settings.db_path, buy_fee_pct=settings.BUY_FEE_PCT).open()
    await storage.prune_seen(settings.SEEN_TTL_DAYS)
    await storage.prune(time.time(), obs_days=settings.OBS_TTL_DAYS, snap_days=settings.SNAPSHOT_TTL_DAYS,
                        decision_days=settings.DECISIONS_TTL_DAYS)
    return storage


def build_peers(settings: Settings, session=None, clock=time.time):
    from .fx import TonRate
    from .peers import Peers, TelethonTgValue, TgValueService

    fetch = TelethonTgValue(session) if session is not None and settings.TG_VALUE_ENABLED else None
    tg = TgValueService(fetch, ttl=settings.TG_VALUE_TTL, max_per_min=settings.TG_VALUE_MAX_PER_MIN, clock=clock)
    rate = TonRate(settings.TON_RATE_URL, settings.TON_RATE_TTL, clock=clock)
    return Peers(settings, MARKET, tg=tg, rate=rate, clock=clock)


def telegram_session(settings: Settings):
    if not settings.has_tg_credentials:
        return None
    from .tg_auth import TelegramSession

    return TelegramSession(settings)


def make_engine(settings: Settings, storage, client, notifier, session=None, **kw):
    from .engine import Engine
    from .events import EventLog

    return Engine(
        settings, storage, client, notifier,
        peers=build_peers(settings, session),
        events=EventLog(settings.EVENTS_FILE, settings.EVENTS_MAX_MB),
        snapshot_path=Path(settings.DATA_DIR) / f"floors_{MARKET}.json",
        **kw,
    )


async def _build(settings: Settings, session=None):
    """Real client + storage + notifier + engine (practice is forced at start; LIVE needs /dry off)."""
    from .client import PortalsClient
    from .notifier import Notifier
    from .tg_auth import InitDataProvider

    admin_ids = settings.admin_ids  # validate config (may raise ConfigError) before opening anything
    bot = _make_bot(settings)
    if bot is None:
        log.info("BOT_TOKEN is empty: notifications go to stdout")
    notifier = Notifier(bot, admin_ids)
    client = PortalsClient(settings, InitDataProvider(settings, session))
    storage = None
    try:
        storage = await open_storage(settings)
        engine = make_engine(settings, storage, client, notifier, session)
        await engine.start_in_practice()  # LIVE survives a restart only while its 24 h gate is still open
    except BaseException:  # an open aiosqlite connection would keep the process alive: close everything
        if storage is not None:
            await storage.close()
        await client.close()
        await _close_bot(bot)
        raise
    return storage, client, bot, notifier, engine


async def _preflight_live(settings: Settings, engine, client) -> bool:
    """In live mode make sure auth works before the first cycle. Returns the practice flag."""
    dry = await engine.is_dry()
    if not dry:
        settings.require_auth()
        await client.auth.get()
    return dry


# ---------------------------------------------------------------- commands
async def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    from .tg_auth import InitDataProvider, interactive_login

    settings.require("TG_API_ID", "TG_API_HASH")
    print(await interactive_login(settings))
    init_data = await InitDataProvider(settings).get(force=True)
    print(f"Portals initData OK: {mask(init_data, 24)}")
    return 0


async def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    from .bot import AppContext, build_dispatcher, set_bot_commands
    from .doctor import make_tg_probe
    from .tg_auth import lookup_gift

    session = telegram_session(settings)
    storage, client, bot, notifier, engine = await _build(settings, session)
    stop = asyncio.Event()
    dp = None
    try:
        dry = await _preflight_live(settings, engine, client)
        await engine.sync_rules_file()
        n_rules = len(await storage.list_rules(only_enabled=True))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        tasks = {asyncio.create_task(engine.run_forever(stop), name="engine")}
        if bot is not None:
            gift_lookup = (lambda slug: lookup_gift(session, slug)) if session else None
            ctx = AppContext(settings, storage, client, engine, peers=engine.peers, tg_session=session,
                             gift_lookup=gift_lookup, tg_probe=make_tg_probe(settings, session))
            dp = build_dispatcher(ctx)
            await set_bot_commands(bot)
            tasks.add(asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, close_bot_session=False), name="bot"))
        else:
            log.warning("BOT_TOKEN is empty: running without the admin bot")
        await notifier.send(f"Portals autobuy started ({'PRACTICE' if dry else 'LIVE'}), {n_rules} active rule(s). "
                            "/safety for limits, /status for details.")

        stopper = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait(tasks | {stopper}, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        failed = [t for t in done if t is not stopper and not t.cancelled() and t.exception()]
        for t in failed:
            log.error("task %s failed: %s", t.get_name(), t.exception())
        if dp is not None:
            with contextlib.suppress(Exception):
                await dp.stop_polling()
        # Graceful stop: no new buy starts, a buy in flight (loop or tap) gets its answer and its purchase
        # row, and the engine ends its cycle on its own; only then is anything still running cancelled.
        grace = settings.HTTP_TIMEOUT + 5
        if not await engine.drain(grace):
            log.warning("a buy was still in flight after %.0fs; it stays recorded as 'unknown'", grace)
        engine_task = next(t for t in tasks if t.get_name() == "engine")
        await asyncio.wait({engine_task}, timeout=grace)
        for t in tasks:
            t.cancel()
        stopper.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        return 1 if failed else 0
    finally:
        await client.close()
        if session is not None:
            await session.close()
        await storage.close()
        await _close_bot(bot)


async def cmd_once(settings: Settings, args: argparse.Namespace) -> int:
    storage, client, bot, notifier, engine = await _build(settings)
    try:
        dry = await _preflight_live(settings, engine, client)
        await engine.sync_rules_file()
        if not await storage.list_rules(only_enabled=True):
            print("No rules configured: add rules.json or use /addrule or /hunt in the bot.")
            return 0
        report = await engine.run_cycle()
        print(f"once ({'PRACTICE' if dry else 'LIVE'}): {report.summary()}")
        for err in report.rule_errors:
            print(f"  {err}")
        if report.reasons:
            print(f"skip reasons: {report.top_reasons(10)}")
        return 0
    finally:
        await client.close()
        await storage.close()
        await _close_bot(bot)


async def cmd_floors(settings: Settings, args: argparse.Namespace) -> int:
    from .client import PortalsClient

    if args.json:
        snap = Path(settings.DATA_DIR) / f"floors_{MARKET}.json"
        if snap.exists():
            print(snap.read_text(encoding="utf-8"))
            return 0
        from .peers import build_snapshot
        from .pricing import fee_model_from_settings

        client = PortalsClient(settings)
        try:
            colls = await client.collections()
        finally:
            await client.close()
        data = {c.name: {"floor": c.floor_ton, "n": c.listed_count or 0, "models": {}} for c in colls if c.floor_ton}
        print(json.dumps(build_snapshot(MARKET, fee_model_from_settings(settings), data, time.time()), indent=2))
        return 0
    client = PortalsClient(settings)
    try:
        if args.collection:
            coll = await client.resolve_collection(args.collection)
            fee = " · zero commission" if coll.zero_commission else ""
            print(f"{coll.name} ({coll.short_name}): floor {coll.floor_ton} TON{fee}")
            af = await client.attribute_floors(coll.name)
            if af:
                for title, table in (("models", af.models), ("backdrops", af.backdrops), ("symbols", af.symbols)):
                    cheapest = sorted(((v, k) for k, v in table.items() if v > 0))[: args.top]
                    print(f"  {title}: " + ", ".join(f"{k}={v:g}" for v, k in cheapest))
        else:
            for c in sorted(await client.collections(), key=lambda c: c.name.lower()):
                floor = f"{c.floor_ton:g}" if c.floor_ton is not None else "-"
                print(f"{c.name:<24} {floor:>10}")
        return 0
    finally:
        await client.close()


async def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    from .bot import AppContext, doctor_text
    from .client import PortalsClient
    from .doctor import make_tg_probe
    from .notifier import Notifier

    client = PortalsClient(settings)
    try:
        storage = await open_storage(settings)
    except BaseException:
        await client.close()
        raise
    try:
        engine = make_engine(settings, storage, client, Notifier())
        ctx = AppContext(settings, storage, client, engine, peers=engine.peers, tg_probe=make_tg_probe(settings))
        text = await doctor_text(ctx)
        print(text)
        return 1 if "✗" in text else 0
    finally:
        await client.close()
        await storage.close()


# ---------------------------------------------------------------- mock
class MockClock:
    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _mock_settings(settings: Settings, args: argparse.Namespace, mock_dir: Path, now: float) -> Settings:
    """Write the peer fixtures (ts rewritten to now) into the mock dir and point the settings at them."""
    from .peers import load_fixture_snapshot, write_snapshot

    peer_dir = mock_dir / "peers"
    files = []
    for name in ("floors_mrkt.json", "floors_tonnel.json"):
        src = FIXTURES / "peers" / name
        if src.exists():
            dst = peer_dir / name
            write_snapshot(dst, load_fixture_snapshot(src, now))
            files.append(str(dst))
    update = {
        "PEER_FLOOR_FILES": ",".join(files),
        "EVENTS_FILE": str(Path(settings.DATA_DIR) / "mock-events.jsonl"),
        "POLL_INTERVAL": max(settings.POLL_INTERVAL, 1.0),
    }
    if args.shadow_hours is not None:
        update.update(SHADOW_MIN_HOURS=args.shadow_hours, SHADOW_MAX_HOURS=args.shadow_hours)
    return settings.model_copy(update=update)


def _tg_fixture(now: float) -> dict:
    path = FIXTURES / "tg_value.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for slug, info in data.items():
        if slug.startswith("_"):
            continue
        info = dict(info)
        days = info.pop("last_sale_days_ago", None)
        if days is not None:
            info["last_sale_date"] = now - days * 86400
        out[slug] = info
    return out


async def _seed(storage, now: float) -> None:
    """Seed 8 practice buys over 6 days with drifting floors (5 up, 3 down) and a 24 h observation tape."""
    from .ledger import position_key
    from .models import Listing

    def gift(lid: str, model: str, number: int, price: float) -> Listing:
        return Listing(lid, "Lunar Snake", model, "Onyx Black", "Star", number, price,
                       raw={"seed": True, "price": f"{price:g}"})

    albino = position_key("Lunar Snake", "Albino")
    cobra = position_key("Lunar Snake", "Cobra")
    for i, hours_ago in enumerate((144, 120, 96, 72, 48, 24, 12, 2)):
        ts = now - hours_ago * 3600
        await storage.add_snapshot(ts, albino, round(1.30 + 0.25 * i / 7, 3), 4, "cluster")
        await storage.add_snapshot(ts, cobra, round(1.30 - 0.30 * i / 7, 3), 4, "cluster")
    seeds = [(1, "Albino", 1.10, 140), (1, "Albino", 1.15, 118), (1, "Albino", 1.20, 95), (1, "Albino", 1.25, 70),
             (1, "Albino", 1.30, 47), (2, "Cobra", 1.20, 130), (2, "Cobra", 1.35, 100), (2, "Cobra", 1.55, 60)]
    for i, (rule_id, model, price, hours_ago) in enumerate(seeds):
        await storage.record_purchase(gift(f"seed-{i}", model, 4300 + i, price), dry=True, status="dry",
                                      rule_id=rule_id, error="seeded practice", cost_ton=price,
                                      created_at=now - hours_ago * 3600)
    tape = [("tape-1", "Albino", 1.10, 20), ("tape-2", "Albino", 1.65, 18), ("tape-3", "Cobra", 1.00, 16),
            ("tape-4", "Albino", 1.20, 11), ("tape-5", "Albino", 1.62, 9), ("tape-6", "Cobra", 0.98, 8),
            ("tape-7", "Albino", 1.05, 6), ("tape-8", "Albino", 0.90, 4)]
    for lid, model, price, hours_ago in tape:
        await storage.upsert_observations([gift(lid, model, 5000 + int(hours_ago), price)], now - hours_ago * 3600)
    await storage.add_decision(now - 4 * 3600, "tape-8", 0.90, None, "skip", "LOCKED",
                               {"text": "locked: unlocks in 5 d", "title": "Lunar Snake #5004"})


async def _fake_tap(engine, action: str, now: float, tokens: list[str] | None = None) -> None:
    """Simulated taps: run the real tap handler on every pending card, then try one double tap."""
    if tokens is None:
        tokens = [c.token for c in await engine.storage.list_pending(("pending",))]
    first = None
    for token in tokens:
        card = await engine.storage.get_pending(token)
        result = await engine.tap(action[0], token, MOCK_ADMIN_ID, now)
        if card is not None and not result.ok and "Stopped" in result.text:
            print(f"[tap {action}] {card.payload.get('title')}: {result.text}", flush=True)
            continue
        print(f"[tap {action}] {result.text}", flush=True)
        first = first or (card.token if card else None)
    if first and action == "buy":
        again = await engine.tap("b", first, MOCK_ADMIN_ID, now)
        print(f"[tap {action}] double tap on the same card: {again.text}", flush=True)


def _section(title: str) -> None:
    print(f"\n--- {title} ---", flush=True)


async def cmd_mock(settings: Settings, args: argparse.Namespace) -> int:
    from collections import Counter

    from .bot import (AppContext, compare_text, doctor_text, dry_off_gate, peers_text, pnl_text, safety_for,
                      purchase_tag, score_text, why_for)
    from .client import DEMO_RULES, MockClient
    from .digest import build_digest
    from .engine import DAY, Engine
    from .events import EventLog, read_events, validate_event
    from .fx import TonRate
    from .ledger import scoreboard
    from .notifier import Notifier
    from .peers import FakeTgValue, Peers, TgValueService, read_snapshot
    from .rules import parse_addrule_args
    from .wizard import preview_for

    start = time.time()
    clock = MockClock(start)
    mock_dir = Path(settings.DATA_DIR) / "mock"
    s = _mock_settings(settings, args, mock_dir, start)
    events_path = Path(s.EVENTS_FILE)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")

    # Config errors (a malformed BOT_TOKEN or ADMIN_IDS) surface before the database opens: an open
    # aiosqlite connection has a non-daemon thread, and leaving it open would keep the process alive.
    bot = _make_bot(s)
    try:
        admin_ids = s.admin_ids if bot is not None else []
        storage = await open_storage(s, ":memory:")  # fresh state on every mock run
    except BaseException:
        await _close_bot(bot)
        raise
    try:
        notifier = Notifier(bot, admin_ids)
        client = MockClient(now=start, floors_ttl=s.FLOORS_TTL)
        tg = TgValueService(FakeTgValue(_tg_fixture(start)), ttl=s.TG_VALUE_TTL, max_per_min=s.TG_VALUE_MAX_PER_MIN,
                            clock=clock)
        peers = Peers(s, MARKET, tg=tg, rate=TonRate(s.TON_RATE_URL, fixed=1.25), clock=clock)
        for rule in DEMO_RULES:
            await storage.add_rule(replace(rule, id=None))
        if args.shadow_hours is not None:
            await storage.update_rule(1, state="shadow")
            await storage.set_setting("shadow_since:1", start - 6 * DAY)
        await _seed(storage, start)
        engine = Engine(s, storage, client, notifier, force_dry=True, clock=clock, peers=peers,
                        events=EventLog(events_path, s.EVENTS_MAX_MB), snapshot_path=mock_dir / f"floors_{MARKET}.json")
        engine.admin_ids.add(MOCK_ADMIN_ID)
        ctx = AppContext(s, storage, client, engine, peers=peers)
        rules = await storage.list_rules()
        print(f"MOCK mode: {len(rules)} demo rules, {args.cycles} cycle(s), PRACTICE forced (DRY_RUN lock), "
              f"tap={args.tap}")
        for rule in rules:
            print(f"  rule #{rule.id}: {rule.describe()}")
        print(f"  {engine.fees.describe(s.FEES_VERIFIED)}")

        totals: Counter = Counter()
        doctor_report = ""
        for i in range(1, args.cycles + 1):
            _section(f"cycle {i}")
            before = client.requests
            report = await engine.run_cycle()
            totals.update(report.reasons)
            print(f"cycle {i}/{args.cycles}: {report.summary()} · requests={client.requests - before}")
            for line in report.rejections:
                print(f"  {line}")
            tokens = None
            if args.panic and i == 1:
                _section("/panic")
                tokens = [c.token for c in await storage.list_pending(("pending",))]
                await engine.panic()
                print(await safety_for(ctx))
            if args.tap != "none":
                if i == 1 and not args.panic:
                    client.reprice("dc-3002", 2.40)  # someone repriced the confirm card's listing before the tap
                await _fake_tap(engine, args.tap, clock(), tokens)
            if i == args.cycles and args.doctor:
                doctor_report = await doctor_text(ctx)
            client.advance()
            clock.advance(120)
            await engine.tick(marks_search=False)
            if i < args.cycles and args.interval:
                await asyncio.sleep(args.interval)

        if args.crash:
            _section("crash cycle")
            client.crash("Lunar Snake", "Cobra", 0.7)
            report = await engine.run_cycle()
            totals.update(report.reasons)
            print(f"crash cycle: {report.summary()}")
            clock.advance(120)

        now = clock()
        await engine.run_marks(now, search=False)
        _section("/pnl")
        print(await pnl_text(ctx, 7, html_mode=False))
        _section("/score")
        print(await score_text(ctx))
        _section("/dry off (gate, as it would answer with DRY_RUN=false)")
        gate_text, gate_kb = await dry_off_gate(ctx, False, preview=True)
        print(gate_text.split("\n\n")[-1])
        print(f"[buttons: {' | '.join(b.text for row in gate_kb.inline_keyboard for b in row)}]")
        print((await dry_off_gate(ctx, False))[0].split("\n")[0])
        _section("/scoreboard")
        print(await scoreboard(storage, s, await storage.list_rules(only_enabled=True), now, engine.fees))
        _section("/why ls-4412 (the listing skipped because it loses money after fees)")
        print(await why_for(ctx, "ls-4412"))
        _section("digest preview")
        print(await build_digest(engine, now - DAY, now + 1, title="Digest, last 24 h"))
        _section("/safety")
        print(await safety_for(ctx))
        _section("/peers")
        print(await peers_text(ctx))
        _section("/compare Spy Agaric")
        print(await compare_text(ctx, "Spy Agaric", None))

        _section("files")
        snap = read_snapshot(str(mock_dir / f"floors_{MARKET}.json"), "other", now, 10**9)
        print(f"own snapshot {mock_dir / f'floors_{MARKET}.json'}: {snap.status}, "
              f"{len((snap.data or {}).get('collections', {}))} collections")
        events = read_events(events_path)
        bad = sum(1 for e in events if validate_event(e))
        print(f"events {events_path}: {len(events)} lines, {bad} invalid")

        if args.preview:
            _section(f"preview: {args.preview}")
            rule = parse_addrule_args(args.preview)
            print(f"Rule: {rule.describe()}")
            print((await preview_for(ctx, rule, now)).text)
        if args.doctor:
            _section("doctor (mock, right after the last cycle)")
            print(doctor_report or await doctor_text(ctx))

        _section("summary")
        purchases = await storage.recent_purchases(100)
        fresh = [p for p in purchases if p.detail != "seeded practice"]
        print(f"recorded purchases: {len(purchases)} ({len(purchases) - len(fresh)} seeded)")
        for p in reversed(fresh):
            cost = p.cost_ton if p.cost_ton is not None else p.price_ton
            floor = f" (floor {p.floor_ton:.2f})" if p.floor_ton else ""
            print(f"  #{p.id} {purchase_tag(p.dry, p.status)} {p.title} {cost:g} TON all-in{floor}")
        print(f"client.buy() calls: {len(client.bought)} (practice never buys)")
        print("skip reasons: " + " · ".join(f"{k} {v}" for k, v in totals.most_common()))
        return 0
    finally:
        await storage.close()
        await _close_bot(bot)


# ---------------------------------------------------------------- entrypoint
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m portals_autobuy",
        description="Autobuy (sniper) for Telegram gifts on the Portals marketplace, with net-of-fees deal math. "
        "Practice mode (DRY_RUN=true) by default.",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("login", help="interactive Telethon login (phone/code), then test Portals initData")
    sub.add_parser("run", help="run the poll loop + admin bot until Ctrl+C")
    sub.add_parser("once", help="run a single scan cycle against the real API and exit")
    m = sub.add_parser("mock", help="offline run with fixture data (always practice), then exit")
    m.add_argument("--cycles", type=int, default=2, help="number of cycles (default 2)")
    m.add_argument("--interval", type=float, default=0.0, help="seconds between cycles (default 0)")
    m.add_argument("--tap", choices=("none", "buy", "skip"), default="none",
                   help="after each cycle, tap this button on every pending card (buy also tries one double tap)")
    m.add_argument("--crash", action="store_true", help="add a cycle where a floor falls 30%%")
    m.add_argument("--panic", action="store_true", help="press /panic after cycle 1")
    m.add_argument("--preview", metavar="RULE",
                   help='preview a rule on the 24 h tape, e.g. "Lunar Snake profit=0.3 max=3"')
    m.add_argument("--doctor", action="store_true", help="print /doctor against the mock client")
    m.add_argument("--shadow-hours", type=float, default=None, metavar="H",
                   help="start rule #1 in shadow and print its hindsight card after H hours (0 = now)")
    f = sub.add_parser("floors", help="print collection floors (public API, no auth)")
    f.add_argument("collection", nargs="?", help="collection name for per-model/backdrop/symbol floors")
    f.add_argument("--top", type=int, default=8, help="cheapest N attributes to show")
    f.add_argument("--json", action="store_true",
                   help="print this bot's floor snapshot as JSON (the file other bots read via PEER_FLOOR_FILES)")
    sub.add_parser("doctor", help="self-check: config, credentials, API, DB, clock; exit 1 on any failure")
    return p


COMMANDS = {
    "login": cmd_login,
    "run": cmd_run,
    "once": cmd_once,
    "mock": cmd_mock,
    "floors": cmd_floors,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32" and os.environ.get("ALLOW_UNSUPPORTED_OS") != "1":
        print("This build is Windows-only. Install it on Windows 10/11, or set ALLOW_UNSUPPORTED_OS=1 to run it anyway (unsupported).", file=sys.stderr)
        return 1
    _updater.startup()
    args = build_parser().parse_args(argv)
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)  # keep prints in order with logs
    if sys.platform != "win32" and os.environ.get("ALLOW_UNSUPPORTED_OS") != "1":
        print("This build is Windows-only. Install it on Windows 10/11, or set ALLOW_UNSUPPORTED_OS=1 to run it anyway (unsupported).", file=sys.stderr)
        return 1
    try:
        settings = get_settings()
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        print(f"Error: invalid value in .env: {errs}", file=sys.stderr)
        return 2
    setup_logging(settings.LOG_LEVEL)
    from .client import PortalsError

    try:
        return asyncio.run(COMMANDS[args.command](settings, args))
    except (ConfigError, TgAuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except PortalsError as exc:
        print(f"Portals API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
