"""SQLite storage (aiosqlite): seen listings, purchases, rules, runtime settings, plus the
honest-price tables (floor_snapshots, observations, decisions), deal cards (pending, mutes)
and the practice ledger (marks). Old scaffold databases are migrated in place."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Listing, alnum_key, norm
from .rules import STATE_UNSET, BuyRule, file_rule_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    nft_id     TEXT NOT NULL,
    price      TEXT NOT NULL,
    first_seen REAL NOT NULL,
    PRIMARY KEY (nft_id, price)
);
CREATE TABLE IF NOT EXISTS purchases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    nft_id     TEXT NOT NULL,
    collection TEXT,
    model      TEXT,
    backdrop   TEXT,
    symbol     TEXT,
    number     INTEGER,
    price_ton  REAL NOT NULL,
    floor_ton  REAL,
    rule_id    INTEGER,
    dry        INTEGER NOT NULL,
    status     TEXT NOT NULL,          -- dry | ok | error | skipped | unknown (buy() raised; may have executed)
    error      TEXT
);
CREATE INDEX IF NOT EXISTS purchases_ts ON purchases (ts);
CREATE TABLE IF NOT EXISTS rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    collection       TEXT NOT NULL,
    model            TEXT,
    backdrop         TEXT,
    symbol           TEXT,
    max_price_ton    REAL NOT NULL,
    min_discount_pct REAL NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    source           TEXT NOT NULL DEFAULT 'bot',
    created_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS floor_snapshots (
    ts REAL NOT NULL,
    filter_key TEXT NOT NULL,
    floor_ton REAL,
    n INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_key_ts ON floor_snapshots(filter_key, ts);

CREATE TABLE IF NOT EXISTS observations (
    listing_id TEXT NOT NULL,
    price_ton REAL NOT NULL,
    collection TEXT,
    model TEXT,
    backdrop TEXT,
    symbol TEXT,
    number INTEGER,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (listing_id, price_ton)
);
CREATE INDEX IF NOT EXISTS idx_obs_first ON observations(first_seen);
CREATE INDEX IF NOT EXISTS idx_obs_coll ON observations(collection, number);

CREATE TABLE IF NOT EXISTS decisions (
    ts REAL NOT NULL,
    listing_id TEXT NOT NULL,
    price_ton REAL,
    rule_id INTEGER,
    outcome TEXT NOT NULL,          -- skip | alert | dry | bought | failed | muted | near | tap
    reason_code TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_listing ON decisions(listing_id, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS pending (
    token TEXT PRIMARY KEY,
    listing_id TEXT,
    rule_id INTEGER,
    price_ton REAL,
    cost_ton REAL,
    payload_json TEXT,
    msgs_json TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL            -- pending|executing|bought|dry|gone|changed|expired|skipped|watch|near
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending(status, expires_at);

CREATE TABLE IF NOT EXISTS mutes (
    key TEXT PRIMARY KEY,
    until_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    purchase_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    floor_ton REAL,
    net_exit_ton REAL,
    confidence TEXT,
    PRIMARY KEY (purchase_id, ts)
);
"""

# Columns added after the scaffold: (table, column, declaration). Applied with guarded ALTERs.
ADDED_COLUMNS = (
    ("rules", "min_profit_ton", "REAL NOT NULL DEFAULT 0"),
    ("rules", "min_profit_pct", "REAL NOT NULL DEFAULT 0"),
    ("rules", "min_confidence", "TEXT NOT NULL DEFAULT 'med'"),
    ("rules", "allow_locked_days", "REAL NOT NULL DEFAULT 0"),
    ("rules", "mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("rules", "exit", "TEXT NOT NULL DEFAULT 'here'"),
    ("rules", "state", "TEXT NOT NULL DEFAULT 'live'"),
    ("rules", "promoted_at", "REAL"),
    ("purchases", "cost_ton", "REAL"),
    ("purchases", "model", "TEXT"),
    ("purchases", "backdrop", "TEXT"),
    ("purchases", "symbol", "TEXT"),
    ("purchases", "closed_at", "REAL"),
    ("purchases", "exit_price_ton", "REAL"),
    ("purchases", "exit_note", "TEXT"),
)

RULE_FIELDS = (
    "collection", "model", "backdrop", "symbol", "max_price_ton", "min_discount_pct", "enabled", "source",
    "min_profit_ton", "min_profit_pct", "min_confidence", "allow_locked_days", "mode", "exit", "state", "promoted_at",
)

# Statuses that count toward DAILY_BUDGET_TON and MAX_BUYS_PER_HOUR: a buy with an unknown result
# (timeout, network error, 5xx) may have gone through on the server, so it counts until proven otherwise.
SPEND_STATUSES = "('ok', 'dry', 'unknown')"


@dataclass(slots=True)
class Snapshot:
    ts: float
    filter_key: str
    floor_ton: float | None
    n: int
    source: str


@dataclass(slots=True)
class Decision:
    ts: float
    listing_id: str
    price_ton: float | None
    rule_id: int | None
    outcome: str
    reason_code: str | None
    detail: dict[str, Any]


@dataclass(slots=True)
class PendingCard:
    token: str
    listing_id: str
    rule_id: int | None
    price_ton: float
    cost_ton: float
    payload: dict[str, Any]
    msgs: list[tuple[int, int]]
    created_at: float
    expires_at: float
    status: str


@dataclass(slots=True)
class Position:
    id: int
    listing_id: str
    collection: str
    model: str | None
    title: str
    price_ton: float
    cost_ton: float
    rule_id: int | None
    dry: bool
    status: str
    created_at: float
    closed_at: float | None
    exit_price_ton: float | None
    mark_ts: float | None = None
    mark_floor: float | None = None
    mark_net: float | None = None
    mark_conf: str | None = None


@dataclass(frozen=True, slots=True)
class Purchase:
    id: int
    ts: float
    nft_id: str
    collection: str | None
    model: str | None
    backdrop: str | None
    symbol: str | None
    number: int | None
    price_ton: float
    floor_ton: float | None
    rule_id: int | None
    dry: bool
    status: str
    error: str | None
    cost_ton: float | None = None

    @property
    def title(self) -> str:
        return f"{self.collection} #{self.number}" if self.number is not None else (self.collection or self.nft_id)

    @property
    def detail(self) -> str:
        """Note stored with the purchase (skip reason, buy error, 'SHADOW_RULE', 'tap', ...)."""
        return self.error or ""


def _title(collection: str | None, number: int | None, fallback: str) -> str:
    if collection and number is not None:
        return f"{collection} #{number}"
    return collection or fallback


class Storage:
    def __init__(self, path: str | Path, *, buy_fee_pct: float = 0.0):
        self.path = str(path)
        self.buy_fee_pct = buy_fee_pct  # used once to backfill purchases.cost_ton on old databases
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> "Storage":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()
        return self

    async def _columns(self, table: str) -> set[str]:
        async with self.db.execute(f"PRAGMA table_info({table})") as cur:
            return {row[1] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        """Guarded ALTER TABLE ... ADD COLUMN for scaffold databases; keeps rules and purchases."""
        cache: dict[str, set[str]] = {}
        added: set[tuple[str, str]] = set()
        for table, column, decl in ADDED_COLUMNS:
            if table not in cache:
                cache[table] = await self._columns(table)
            if column not in cache[table]:
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                cache[table].add(column)
                added.add((table, column))
        if ("purchases", "cost_ton") in added:
            await self.db.execute(
                "UPDATE purchases SET cost_ton = price_ton * (1 + ? / 100.0) WHERE cost_ton IS NULL",
                (self.buy_fee_pct,),
            )

    async def prune(self, now: float, *, obs_days: int = 14, snap_days: int = 14, decision_days: int = 7) -> None:
        await self.db.execute("DELETE FROM floor_snapshots WHERE ts < ?", (now - snap_days * 86400,))
        await self.db.execute("DELETE FROM observations WHERE last_seen < ?", (now - obs_days * 86400,))
        await self.db.execute("DELETE FROM decisions WHERE ts < ?", (now - decision_days * 86400,))
        await self.db.execute("DELETE FROM mutes WHERE until_ts < ?", (now,))
        await self.db.execute("DELETE FROM pending WHERE created_at < ?", (now - 7 * 86400,))
        await self.db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "Storage":
        return await self.open()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not open")
        return self._db

    # ---------- seen ----------
    async def mark_seen(self, listing: Listing) -> bool:
        """Insert (id, price). Returns True if it was new (a relist at a new price counts as new)."""
        async with self.db.execute(
            "SELECT 1 FROM seen WHERE nft_id = ? AND price = ?", (listing.id, listing.price_str)
        ) as cur:
            if await cur.fetchone():
                return False  # hot path: no write, no commit
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO seen (nft_id, price, first_seen) VALUES (?, ?, ?)",
            (listing.id, listing.price_str, time.time()),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def prune_seen(self, older_than_days: float) -> int:
        cur = await self.db.execute(
            "DELETE FROM seen WHERE first_seen < ?", (time.time() - older_than_days * 86400,)
        )
        await self.db.commit()
        return cur.rowcount

    # ---------- purchases ----------
    async def record_purchase(
        self,
        listing: Listing,
        *,
        dry: bool,
        status: str,
        floor: float | None = None,
        rule_id: int | None = None,
        error: str | None = None,
        cost_ton: float | None = None,
        created_at: float | None = None,
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO purchases (ts, nft_id, collection, model, backdrop, symbol, number, "
            "price_ton, floor_ton, rule_id, dry, status, error, cost_ton) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time() if created_at is None else created_at,
                listing.id,
                listing.collection,
                listing.model,
                listing.backdrop,
                listing.symbol,
                listing.number,
                listing.price_ton,
                floor,
                rule_id,
                int(dry),
                status,
                error,
                listing.price_ton if cost_ton is None else cost_ton,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def settle_purchase(self, purchase_id: int, *, status: str, error: str | None) -> bool:
        """Final status of a live buy that was recorded as 'unknown' before the order was sent."""
        cur = await self.db.execute("UPDATE purchases SET status=?, error=? WHERE id=?", (status, error, purchase_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def spent_since(self, since_ts: float, *, dry: bool) -> float:
        """All-in spend (cost incl. buy fee) of successful (or possibly successful) buys since `since_ts`."""
        async with self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_ton, price_ton)), 0) FROM purchases WHERE ts >= ? AND dry = ? "
            f"AND status IN {SPEND_STATUSES}",
            (since_ts, int(dry)),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0] if row else 0)

    async def buys_since(self, since_ts: float, *, dry: bool) -> int:
        async with self.db.execute(
            f"SELECT COUNT(*) FROM purchases WHERE ts >= ? AND dry = ? AND status IN {SPEND_STATUSES}",
            (since_ts, int(dry)),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def recent_purchases(self, limit: int = 10) -> list[Purchase]:
        async with self.db.execute(
            "SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            Purchase(
                id=r["id"],
                ts=r["ts"],
                nft_id=r["nft_id"],
                collection=r["collection"],
                model=r["model"],
                backdrop=r["backdrop"],
                symbol=r["symbol"],
                number=r["number"],
                price_ton=r["price_ton"],
                floor_ton=r["floor_ton"],
                rule_id=r["rule_id"],
                dry=bool(r["dry"]),
                status=r["status"],
                error=r["error"],
                cost_ton=r["cost_ton"],
            )
            for r in rows
        ]

    # ---------- practice ledger ----------
    @staticmethod
    def _position(r: aiosqlite.Row) -> Position:
        keys = r.keys()
        return Position(
            id=r["id"],
            listing_id=r["nft_id"],
            collection=r["collection"] or "",
            model=r["model"],
            title=_title(r["collection"], r["number"], r["nft_id"]),
            price_ton=r["price_ton"],
            cost_ton=r["cost_ton"] if r["cost_ton"] is not None else r["price_ton"],
            rule_id=r["rule_id"],
            dry=bool(r["dry"]),
            status=r["status"],
            created_at=r["ts"],
            closed_at=r["closed_at"],
            exit_price_ton=r["exit_price_ton"],
            mark_ts=r["mark_ts"] if "mark_ts" in keys else None,
            mark_floor=r["mark_floor"] if "mark_floor" in keys else None,
            mark_net=r["mark_net"] if "mark_net" in keys else None,
            mark_conf=r["mark_conf"] if "mark_conf" in keys else None,
        )

    _POSITIONS_SQL = (
        "SELECT p.*, m.ts AS mark_ts, m.floor_ton AS mark_floor, m.net_exit_ton AS mark_net, m.confidence AS mark_conf"
        " FROM purchases p LEFT JOIN marks m ON m.purchase_id = p.id"
        " AND m.ts = (SELECT MAX(ts) FROM marks WHERE purchase_id = p.id)"
    )

    async def positions(self, *, since: float | None = None, dry: bool | None = None) -> list[Position]:
        """Successful buys (dry or ok) with their latest mark."""
        sql = self._POSITIONS_SQL + " WHERE p.status IN ('dry', 'ok')"
        args: list[Any] = []
        if since is not None:
            sql += " AND p.ts >= ?"
            args.append(since)
        if dry is not None:
            sql += " AND p.dry = ?"
            args.append(int(dry))
        async with self.db.execute(sql + " ORDER BY p.id", args) as cur:
            return [self._position(r) for r in await cur.fetchall()]

    async def open_positions(self, now: float, paper_horizon_days: float = 7) -> list[Position]:
        sql = (
            self._POSITIONS_SQL + " WHERE p.status IN ('dry', 'ok') AND p.closed_at IS NULL"
            " AND (p.dry = 0 OR p.ts >= ?) ORDER BY p.id"
        )
        async with self.db.execute(sql, (now - paper_horizon_days * 86400,)) as cur:
            return [self._position(r) for r in await cur.fetchall()]

    async def get_position(self, purchase_id: int) -> Position | None:
        async with self.db.execute(self._POSITIONS_SQL + " WHERE p.id = ?", (purchase_id,)) as cur:
            row = await cur.fetchone()
        return self._position(row) if row else None

    async def close_position(self, purchase_id: int, exit_price: float, note: str, now: float) -> bool:
        cur = await self.db.execute(
            "UPDATE purchases SET closed_at=?, exit_price_ton=?, exit_note=? WHERE id=? AND status IN ('dry','ok')",
            (now, exit_price, note, purchase_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def add_mark(self, purchase_id: int, ts: float, floor: float, net_exit: float, confidence: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO marks(purchase_id, ts, floor_ton, net_exit_ton, confidence) VALUES (?,?,?,?,?)",
            (purchase_id, ts, floor, net_exit, confidence),
        )
        await self.db.commit()

    async def last_live_positions(self, n: int) -> list[Position]:
        sql = self._POSITIONS_SQL + " WHERE p.status = 'ok' AND p.dry = 0 ORDER BY p.id DESC LIMIT ?"
        async with self.db.execute(sql, (n,)) as cur:
            return [self._position(r) for r in await cur.fetchall()]

    # ---------- floor snapshots ----------
    @staticmethod
    def _snapshot(r: aiosqlite.Row) -> Snapshot:
        return Snapshot(r["ts"], r["filter_key"], r["floor_ton"], r["n"] or 0, r["source"] or "")

    async def add_snapshot(self, ts: float, filter_key: str, floor: float | None, n: int, source: str) -> None:
        await self.db.execute(
            "INSERT INTO floor_snapshots(ts, filter_key, floor_ton, n, source) VALUES (?,?,?,?,?)",
            (ts, filter_key, floor, n, source),
        )
        await self.db.commit()

    async def snapshots(self, filter_key: str, since: float, until: float | None = None) -> list[Snapshot]:
        sql = "SELECT * FROM floor_snapshots WHERE filter_key=? AND ts >= ?"
        args: list[Any] = [filter_key, since]
        if until is not None:
            sql += " AND ts <= ?"
            args.append(until)
        async with self.db.execute(sql + " ORDER BY ts", args) as cur:
            return [self._snapshot(r) for r in await cur.fetchall()]

    async def latest_snapshot(self, filter_key: str, newer_than: float = 0.0, before: float | None = None) -> Snapshot | None:
        sql = "SELECT * FROM floor_snapshots WHERE filter_key=? AND ts >= ? AND floor_ton IS NOT NULL"
        args: list[Any] = [filter_key, newer_than]
        if before is not None:
            sql += " AND ts <= ?"
            args.append(before)
        async with self.db.execute(sql + " ORDER BY ts DESC LIMIT 1", args) as cur:
            r = await cur.fetchone()
        return self._snapshot(r) if r else None

    async def all_snapshots(self, since: float) -> list[Snapshot]:
        async with self.db.execute("SELECT * FROM floor_snapshots WHERE ts >= ? ORDER BY ts", (since,)) as cur:
            return [self._snapshot(r) for r in await cur.fetchall()]

    # ---------- observations ----------
    async def upsert_observations(self, listings: list[Listing], now: float) -> None:
        await self.db.executemany(
            "INSERT INTO observations(listing_id, price_ton, collection, model, backdrop, symbol, number, first_seen,"
            " last_seen) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(listing_id, price_ton) DO UPDATE SET"
            " last_seen=excluded.last_seen",
            [(x.id, x.price_ton, x.collection, x.model, x.backdrop, x.symbol, x.number, now, now) for x in listings],
        )
        await self.db.commit()

    async def observations_since(self, since: float, collection: str | None = None) -> list[dict[str, Any]]:
        async with self.db.execute("SELECT * FROM observations WHERE first_seen >= ? ORDER BY first_seen", (since,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if collection is not None:
            rows = [r for r in rows if norm(r["collection"]) == norm(collection)]
        return rows

    async def observation_bounds(self) -> tuple[float | None, float | None]:
        async with self.db.execute("SELECT MIN(first_seen), MAX(last_seen) FROM observations") as cur:
            row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    async def last_seen(self, listing_id: str) -> float | None:
        async with self.db.execute("SELECT MAX(last_seen) FROM observations WHERE listing_id=?", (listing_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def listing_ids_for(self, slug_name: str, number: int) -> list[str]:
        """Listing ids seen for a t.me/nft/<Slug>-<number> gift (slug matched on alphanumerics)."""
        async with self.db.execute(
            "SELECT DISTINCT listing_id, collection FROM observations WHERE number=?", (number,)
        ) as cur:
            rows = await cur.fetchall()
        key = alnum_key(slug_name)
        return [r["listing_id"] for r in rows if alnum_key(r["collection"]) == key]

    # ---------- decisions ----------
    async def add_decision(
        self, ts: float, listing_id: str, price: float | None, rule_id: int | None, outcome: str,
        reason_code: str | None, detail: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO decisions(ts, listing_id, price_ton, rule_id, outcome, reason_code, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts, listing_id, price, rule_id, outcome, reason_code, json.dumps(detail or {}, default=str)),
        )
        await self.db.commit()

    @staticmethod
    def _decision(r: aiosqlite.Row) -> Decision:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except ValueError:
            detail = {}
        return Decision(r["ts"], r["listing_id"], r["price_ton"], r["rule_id"], r["outcome"], r["reason_code"], detail)

    async def decisions_for(self, listing_id: str, limit: int = 10) -> list[Decision]:
        async with self.db.execute(
            "SELECT * FROM decisions WHERE listing_id=? ORDER BY ts DESC, rowid DESC LIMIT ?", (listing_id, limit)
        ) as cur:
            return [self._decision(r) for r in await cur.fetchall()]

    async def decisions_since(self, since: float, until: float | None = None) -> list[Decision]:
        sql, args = "SELECT * FROM decisions WHERE ts >= ?", [since]
        if until is not None:
            sql += " AND ts < ?"
            args.append(until)
        async with self.db.execute(sql + " ORDER BY ts", args) as cur:
            return [self._decision(r) for r in await cur.fetchall()]

    async def reason_counts(self, since: float, until: float | None = None) -> dict[str, int]:
        sql = "SELECT reason_code, COUNT(*) FROM decisions WHERE ts >= ? AND reason_code IS NOT NULL"
        args: list[Any] = [since]
        if until is not None:
            sql += " AND ts < ?"
            args.append(until)
        async with self.db.execute(sql + " AND outcome IN ('skip','muted') GROUP BY reason_code", args) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}

    # ---------- pending deal cards ----------
    async def create_pending(
        self, token: str, listing_id: str, rule_id: int | None, price: float, cost: float,
        payload: dict[str, Any], now: float, ttl: float, status: str = "pending",
    ) -> None:
        await self.db.execute(
            "INSERT INTO pending(token, listing_id, rule_id, price_ton, cost_ton, payload_json, msgs_json, created_at,"
            " expires_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, listing_id, rule_id, price, cost, json.dumps(payload, default=str), "[]", now, now + ttl, status),
        )
        await self.db.commit()

    @staticmethod
    def _pending(r: aiosqlite.Row) -> PendingCard:
        return PendingCard(
            token=r["token"], listing_id=r["listing_id"], rule_id=r["rule_id"], price_ton=r["price_ton"],
            cost_ton=r["cost_ton"], payload=json.loads(r["payload_json"] or "{}"),
            msgs=[tuple(m) for m in json.loads(r["msgs_json"] or "[]")], created_at=r["created_at"],
            expires_at=r["expires_at"], status=r["status"],
        )

    async def get_pending(self, token: str) -> PendingCard | None:
        async with self.db.execute("SELECT * FROM pending WHERE token=?", (token,)) as cur:
            row = await cur.fetchone()
        return self._pending(row) if row else None

    async def set_pending_msgs(self, token: str, msgs: list[tuple[int, int]]) -> None:
        await self.db.execute("UPDATE pending SET msgs_json=? WHERE token=?", (json.dumps(msgs), token))
        await self.db.commit()

    async def claim_pending(self, token: str, now: float) -> bool:
        """Atomic pending -> executing; False when expired or already handled (double tap safe)."""
        cur = await self.db.execute(
            "UPDATE pending SET status='executing' WHERE token=? AND status='pending' AND expires_at > ?",
            (token, now),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def set_pending_status(self, token: str, status: str, *, only_from: tuple[str, ...] = ()) -> bool:
        sql, args = "UPDATE pending SET status=? WHERE token=?", [status, token]
        if only_from:
            sql += f" AND status IN ({','.join('?' * len(only_from))})"
            args.extend(only_from)
        cur = await self.db.execute(sql, args)
        await self.db.commit()
        return cur.rowcount > 0

    async def list_pending(self, statuses: tuple[str, ...] = ("pending",)) -> list[PendingCard]:
        sql = f"SELECT * FROM pending WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY created_at"
        async with self.db.execute(sql, statuses) as cur:
            return [self._pending(r) for r in await cur.fetchall()]

    async def expire_pending(self, now: float | None = None, *, rule_id: int | None = None) -> list[PendingCard]:
        """Mark due (or, with now=None, all) pending cards expired, optionally only one rule's; returns them for editing."""
        sql, args = "SELECT * FROM pending WHERE status='pending'", []
        if now is not None:
            sql += " AND expires_at <= ?"
            args.append(now)
        if rule_id is not None:
            sql += " AND rule_id = ?"
            args.append(rule_id)
        async with self.db.execute(sql, args) as cur:
            cards = [self._pending(r) for r in await cur.fetchall()]
        for card in cards:
            await self.db.execute("UPDATE pending SET status='expired' WHERE token=? AND status='pending'", (card.token,))
        await self.db.commit()
        return cards

    async def cancel_executing(self) -> int:
        """/panic: taps in flight (status executing) are flagged so their last check before buy() refuses."""
        cur = await self.db.execute("UPDATE pending SET status='expired' WHERE status='executing'")
        await self.db.commit()
        return cur.rowcount

    async def pending_by_message(self, chat_id: int, message_id: int) -> PendingCard | None:
        async with self.db.execute("SELECT * FROM pending ORDER BY created_at DESC LIMIT 500") as cur:
            for r in await cur.fetchall():
                card = self._pending(r)
                if (chat_id, message_id) in card.msgs:
                    return card
        return None

    # ---------- mutes ----------
    async def add_mute(self, key: str, until_ts: float) -> None:
        await self.db.execute(
            "INSERT INTO mutes(key, until_ts) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET until_ts=excluded.until_ts",
            (key, until_ts),
        )
        await self.db.commit()

    async def remove_mute(self, key: str) -> bool:
        cur = await self.db.execute("DELETE FROM mutes WHERE key=?", (key,))
        await self.db.commit()
        return cur.rowcount > 0

    async def muted_key(self, keys: list[str], now: float) -> str | None:
        if not keys:
            return None
        sql = f"SELECT key FROM mutes WHERE until_ts > ? AND key IN ({','.join('?' * len(keys))}) LIMIT 1"
        async with self.db.execute(sql, [now, *keys]) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def list_mutes(self, now: float) -> list[tuple[str, float]]:
        async with self.db.execute("SELECT key, until_ts FROM mutes WHERE until_ts > ? ORDER BY key", (now,)) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    # ---------- rules ----------
    async def add_rule(self, rule: BuyRule) -> int:
        values = [getattr(rule, f) for f in RULE_FIELDS]
        values[RULE_FIELDS.index("enabled")] = int(rule.enabled)
        cur = await self.db.execute(
            f"INSERT INTO rules({', '.join(RULE_FIELDS)}, created_at) VALUES ({', '.join('?' * len(RULE_FIELDS))}, ?)",
            (*values, time.time()),
        )
        await self.db.commit()
        rule.id = int(cur.lastrowid or 0)
        return rule.id

    async def update_rule(self, rule_id: int, **fields: Any) -> bool:
        bad = set(fields) - set(RULE_FIELDS)
        if bad:
            raise ValueError(f"unknown rule fields: {sorted(bad)}")
        if not fields:
            return False
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        sql = "UPDATE rules SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?"
        cur = await self.db.execute(sql, (*fields.values(), rule_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def get_rule(self, rule_id: int) -> BuyRule | None:
        async with self.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)) as cur:
            row = await cur.fetchone()
        return self._rule(row) if row else None

    async def rule_created_at(self, rule_id: int) -> float | None:
        async with self.db.execute("SELECT created_at FROM rules WHERE id=?", (rule_id,)) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else None

    @staticmethod
    def _rule(r: aiosqlite.Row) -> BuyRule:
        return BuyRule(
            id=r["id"],
            collection=r["collection"],
            model=r["model"],
            backdrop=r["backdrop"],
            symbol=r["symbol"],
            max_price_ton=r["max_price_ton"],
            min_discount_pct=r["min_discount_pct"],
            enabled=bool(r["enabled"]),
            source=r["source"],
            min_profit_ton=r["min_profit_ton"] or 0.0,
            min_profit_pct=r["min_profit_pct"] or 0.0,
            min_confidence=r["min_confidence"] or "med",
            allow_locked_days=r["allow_locked_days"] or 0.0,
            mode=r["mode"] or "auto",
            exit=r["exit"] or "here",
            state=r["state"] or "live",
            promoted_at=r["promoted_at"],
        )

    async def delete_rule(self, rule_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def replace_rules_from_source(self, source: str, rules: list[BuyRule], *, new_state: str = "live") -> int:
        """rules.json is the source of truth for its own rules, and each one keeps its id across restarts.

        A file rule is matched to its stored row by its filter (collection, model, backdrop, symbol; a
        repeated filter is told apart by its order in the file) and updated in place; rules that left the
        file are deleted, new ones inserted. Everything keyed by rule id (practice verdicts and the go-live
        gate, the scoreboard, crash pauses, shadow clocks, pending cards) survives `run` and `once`.

        A rule whose file entry has no "state" (STATE_UNSET) starts in `new_state` (shadow when
        SHADOW_NEW_RULES) when it is new, and keeps its stored state (a promotion to live, a pause) when it
        already exists. Only an explicit "state" in the file overwrites the stored one."""
        stored = [r for r in await self.list_rules() if r.source == source]
        by_key = {key: r.id for r, key in zip(stored, file_rule_keys(stored)) if r.id is not None}
        keep: set[int] = set()
        for rule, key in zip(rules, file_rule_keys(rules)):
            rule.source = source
            rule_id = by_key.get(key)
            state_unset = rule.state == STATE_UNSET
            if rule_id is None:
                if state_unset:
                    rule.state = new_state
                keep.add(await self.add_rule(rule))
                continue
            rule.id = rule_id
            skip = {"promoted_at"} | ({"state"} if state_unset else set())
            await self.update_rule(rule_id, **{f: getattr(rule, f) for f in RULE_FIELDS if f not in skip})
            if state_unset:
                current = await self.get_rule(rule_id)
                rule.state = current.state if current else new_state
            keep.add(rule_id)
        for rule_id in set(by_key.values()) - keep:
            await self.delete_rule(rule_id)
        return len(rules)

    async def list_rules(self, *, only_enabled: bool = False) -> list[BuyRule]:
        sql = "SELECT * FROM rules"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        async with self.db.execute(sql) as cur:
            rows = await cur.fetchall()
        return [self._rule(r) for r in rows]

    # ---------- settings ----------
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await self.db.commit()

    async def get_bool(self, key: str, default: bool) -> bool:
        v = await self.get_setting(key)
        if v is None:
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set_setting(key, "1" if value else "0")

    async def get_flag(self, key: str) -> bool | None:
        value = await self.get_setting(key)
        return None if value is None else value.strip().lower() in ("1", "true", "yes", "on")

    async def set_flag(self, key: str, value: bool) -> None:
        await self.set_bool(key, value)

    async def take_setting(self, key: str, value: str) -> bool:
        """Delete `key` only if it still holds `value`; True for exactly one caller (one-time tokens)."""
        cur = await self.db.execute("DELETE FROM settings WHERE key=? AND value=?", (key, value))
        await self.db.commit()
        return cur.rowcount == 1

    async def delete_setting(self, key: str) -> None:
        await self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        await self.db.commit()

    async def get_float(self, key: str) -> float | None:
        value = await self.get_setting(key)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None

    async def settings_with_prefix(self, prefix: str) -> dict[str, str]:
        async with self.db.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}
