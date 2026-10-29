"""Settings loaded from environment / `.env` (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """A required setting is missing or invalid. Message names the `.env` key."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        env_ignore_empty=True,  # `TG_API_ID=` copied from .env.example means "not set", not an invalid int
        # nan/inf are refused for every number: a NaN limit would make every limit comparison False (fail open)
        allow_inf_nan=False,
    )

    # --- Telegram admin bot ---
    BOT_TOKEN: str = ""
    ADMIN_IDS: str = ""  # comma-separated Telegram user ids

    # --- Telegram user account (only for Mini App initData, Telegram's gift valuation, /hunt link lookup) ---
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION_NAME: str = "portals"
    PORTALS_BOT_USERNAME: str = "portals"
    PORTALS_APP_SHORT_NAME: str = "market"
    PORTALS_WEBAPP_URL: str = "https://portal-market.com"
    # Optional manual initData (without the "tma " prefix). Skips Telethon; cannot auto-refresh.
    PORTALS_INIT_DATA: str = ""
    INIT_DATA_TTL: int = 1800  # TODO(verify): server-side initData lifetime unknown; 401/403 forces refresh

    # --- Portals API ---
    PORTALS_API_URL: str = "https://portal-market.com/api"
    HTTP_PROXY: str = ""  # e.g. http://user:pass@host:port
    HTTP_TIMEOUT: float = 15.0
    # Client-side throttle. Server sends x-ratelimit-limit: 2 on /nfts/search; 2.0 still hit 429s live.
    # TODO(verify): limits for authorized requests may differ.
    REQUESTS_PER_SECOND: float = 1.5
    SEARCH_LIMIT: int = 20
    FLOORS_TTL: float = 60.0  # cache for collection / attribute floors, seconds (also the snapshot cadence)

    # --- Safety & limits ---
    # DRY_RUN=true is a hard lock: no bot command or stored setting can enable live buys.
    DRY_RUN: bool = True
    MAX_PRICE_TON: float = 10.0
    DAILY_BUDGET_TON: float = 30.0
    MAX_BUYS_PER_HOUR: int = 3

    # --- Live gate, practice ledger and breakers ---
    LIVE_ARM_HOURS: float = 24.0  # /dry off arms LIVE for this long, then the bot flips back to practice
    PAPER_HORIZON_DAYS: int = 7
    MARK_MAX_AGE_SEC: int = 1800
    MARK_MAX_QUERIES: int = 10
    MARK_INTERVAL_SEC: int = 3600
    GATE_MIN_TRADES: int = 5
    GATE_DAYS: int = 7
    CRASH_PCT: float = 20.0
    CRASH_PAUSE_MIN: int = 60
    LOSS_STREAK_N: int = 3
    LOSS_STREAK_PCT: float = 10.0
    BALANCE_GUARD: bool = True
    BALANCE_TOLERANCE_TON: float = 0.5
    AUTO_PAUSE_ERRORS: int = 5  # consecutive failed live buys -> pause
    STALE_DATA_SEC: int = 300  # warn once when no cycle succeeded for this long
    CONFIRM_ABOVE_TON: float = 0.0  # buys costing more always ask first; 0 = 0.5 x the max price in force
    HOT_WALLET_WARN_X: float = 3.0
    DIGEST_HOUR: int = -1  # local hour for the daily digest, -1 = off
    TZ_OFFSET_HOURS: int = 0  # your UTC offset for displayed times, DIGEST_HOUR and "today"; 0 = UTC

    # --- Fees (net math). TODO(verify): Portals fee schedule; defaults are assumptions ---
    # Guides say a 2.5-5% seller fee; the conservative 5% is used. Collections that Portals marks
    # zero_commission (public /api/collections) get a 0% sell fee automatically.
    BUY_FEE_PCT: float = 0.0
    SELL_FEE_PCT: float = 5.0
    WITHDRAW_FEE_TON: float = 0.1  # TODO(verify): Portals withdrawal fee
    FEES_VERIFIED: bool = False

    # --- Robust floor and trap filter ---
    CLUSTER_K: int = 2
    CLUSTER_BAND_PCT: float = 10.0
    MIN_COMPARABLES_COLLECTION: int = 5
    MIN_COMPARABLES_ATTR: int = 3
    FLOOR_JUMP_PCT: float = 25.0
    TOO_GOOD_PCT: float = 25.0
    OBS_TTL_DAYS: int = 14
    SNAPSHOT_TTL_DAYS: int = 14
    DECISIONS_TTL_DAYS: int = 7

    # --- Deal cards ---
    CONFIRM_TTL_SEC: int = 90
    NEAR_MISS_PCT: float = 5.0
    NEAR_MISS_MAX_PER_HOUR: int = 3
    PREVIEW_HOURS: int = 24
    DISPLAY_CURRENCY: str = "usd"  # usd | none

    # --- Second opinion: Telegram valuation and sibling markets ---
    PEERS_TIMEOUT_SEC: float = 3.0
    TG_VALUE_ENABLED: bool = True
    TG_VALUE_TTL: int = 600
    TG_VALUE_MAX_PER_MIN: int = 10
    TG_VALUE_GUARD: bool = True
    TON_RATE_URL: str = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd"
    TON_RATE_TTL: int = 600
    PEER_FLOOR_FILES: str = ""  # comma-separated floors_<market>.json paths of sibling bots
    PEER_MAX_AGE_SEC: int = 600
    CHEAPER_ELSEWHERE_PCT: float = 3.0

    # --- Shadow rules, events, digest, optional LLM ---
    SHADOW_NEW_RULES: bool = True
    SHADOW_MIN_HOURS: float = 24.0
    SHADOW_MIN_MATCHES: int = 5
    SHADOW_MAX_HOURS: float = 168.0
    SNIPED_WINDOW_SEC: int = 120
    EVENTS_FILE: str = "data/events.jsonl"
    EVENTS_MAX_MB: float = 20.0
    PEER_EVENT_FILES: str = ""
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5-5"

    # --- Engine ---
    POLL_INTERVAL: float = 2.0
    MAX_BACKOFF: float = 60.0
    RULES_FILE: str = "rules.json"
    DATA_DIR: str = "data"
    DB_FILE: str = "portals_autobuy.db"
    SEEN_TTL_DAYS: int = 7
    LOG_LEVEL: str = "INFO"

    # ---------- helpers ----------
    @property
    def admin_ids(self) -> list[int]:
        out: list[int] = []
        for part in self.ADMIN_IDS.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError as exc:
                raise ConfigError(f"ADMIN_IDS must be comma-separated integers, got {part!r}") from exc
        return out

    @property
    def data_dir(self) -> Path:
        p = Path(self.DATA_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.DB_FILE

    @property
    def session_path(self) -> Path:
        # Telethon appends ".session" itself.
        return Path(self.DATA_DIR) / self.TG_SESSION_NAME

    @property
    def session_file(self) -> Path:
        return self.session_path.with_name(self.session_path.name + ".session")

    @property
    def has_tg_credentials(self) -> bool:
        return bool(self.TG_API_ID and self.TG_API_HASH)

    @property
    def can_authenticate(self) -> bool:
        return bool(self.PORTALS_INIT_DATA) or self.has_tg_credentials

    @property
    def floors_ttl(self) -> float:
        """FLOORS_TTL: floors refresh and floor-snapshot cadence."""
        return self.FLOORS_TTL

    @property
    def confirm_above_ton(self) -> float:
        return self.confirm_above(self.MAX_PRICE_TON)

    def confirm_above(self, max_price_ton: float) -> float:
        """Buys costing more always ask first: CONFIRM_ABOVE_TON, or by default half of the max price in
        force (pass the effective max, so a /preset that lowers the max lowers this threshold too)."""
        return self.CONFIRM_ABOVE_TON if self.CONFIRM_ABOVE_TON > 0 else 0.5 * max_price_ton

    @staticmethod
    def _paths(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def peer_floor_files(self) -> list[str]:
        return self._paths(self.PEER_FLOOR_FILES)

    @property
    def peer_event_files(self) -> list[str]:
        return self._paths(self.PEER_EVENT_FILES)

    def require(self, *keys: str) -> None:
        missing = [k for k in keys if not getattr(self, k)]
        if missing:
            raise ConfigError(
                f"Missing setting(s) in .env: {', '.join(missing)}. See .env.example."
            )

    def require_auth(self) -> None:
        if not self.can_authenticate:
            raise ConfigError(
                "Portals auth is not configured: set TG_API_ID and TG_API_HASH in .env "
                "(then run `python -m portals_autobuy login`) or PORTALS_INIT_DATA."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
