"""YAML + BEAT_SPIKE_ env configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

FEED_IDS = frozenset({"chainlink", "binance"})

_ENV_EXECUTION_KEYS = (
    ("BEAT_SPIKE_EXECUTION__API_KEY", "api_key"),
    ("BEAT_SPIKE_EXECUTION__API_SECRET", "api_secret"),
    ("BEAT_SPIKE_EXECUTION__API_PASSPHRASE", "api_passphrase"),
    ("BEAT_SPIKE_EXECUTION__PRIVATE_KEY", "private_key"),
    ("BEAT_SPIKE_EXECUTION__FUNDER", "funder"),
)

_ENV_CHAINLINK_KEYS = (
    ("BEAT_SPIKE_CHAINLINK__STREAMS_USER_ID", "streams_user_id"),
    ("BEAT_SPIKE_CHAINLINK__STREAMS_SECRET", "streams_secret"),
)

# Legacy DAWN_* env names (deprecated; still read as fallback)
_LEGACY_EXECUTION_KEYS = (
    ("DAWN_EXECUTION__API_KEY", "api_key"),
    ("DAWN_EXECUTION__API_SECRET", "api_secret"),
    ("DAWN_EXECUTION__API_PASSPHRASE", "api_passphrase"),
    ("DAWN_EXECUTION__PRIVATE_KEY", "private_key"),
    ("DAWN_EXECUTION__FUNDER", "funder"),
)

_LEGACY_CHAINLINK_KEYS = (
    ("DAWN_CHAINLINK__STREAMS_USER_ID", "streams_user_id"),
    ("DAWN_CHAINLINK__STREAMS_SECRET", "streams_secret"),
)


class BotConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    log_level: str = "INFO"
    use_server_time_sync: bool = True
    server_time_sync_interval_seconds: float = Field(default=3600.0, ge=10.0)
    server_time_sync_samples: int = Field(default=20, ge=3, le=100)
    server_time_sync_keep: int = Field(default=10, ge=1, le=99)
    time_offset_sec: int = 0
    clob_order_warmup_enabled: bool = True
    clob_order_warmup_price: float = Field(default=0.01, gt=0.0, lt=1.0)
    clob_order_warmup_shares: float = Field(default=5.0, gt=0.0)
    fast_order_presign_enabled: bool = True


class ApiConfig(BaseModel):
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_url: str = "wss://ws-live-data.polymarket.com"


class MarketsConfig(BaseModel):
    assets: list[str] = Field(default_factory=lambda: ["btc"])
    intervals: list[str] = Field(default_factory=lambda: ["5m"])
    enabled_assets: list[str] | None = None

    def active_assets(self) -> list[str]:
        if self.enabled_assets:
            return [a.lower().strip() for a in self.enabled_assets]
        return [a.lower().strip() for a in self.assets]


class EntryConfig(BaseModel):
    entry_moment_seconds: float = Field(default=0.0, ge=0.0)
    end_moment_seconds: float = Field(default=60.0, ge=0.0)


class AssetStrategyParams(BaseModel):
    """Per-asset delta detection: |USD Δ| from lookback point to current price vs threshold."""

    delta_threshold_usd: float | None = Field(
        default=None,
        gt=0.0,
        description="Min |Δ| (USD) to trigger UP or DOWN (same both sides)",
    )
    # Legacy: optional per-side overrides (omit in config — use delta_threshold_usd)
    delta_threshold_up_usd: float | None = Field(default=None, gt=0.0)
    delta_threshold_down_usd: float | None = Field(default=None, gt=0.0)
    lookback_seconds: float | None = Field(default=None, gt=0.0)
    sustain_seconds: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _normalize_thresholds(self) -> AssetStrategyParams:
        if self.delta_threshold_up_usd is None and self.delta_threshold_down_usd is None:
            if self.delta_threshold_usd is None:
                raise ValueError("by_asset entry needs delta_threshold_usd")
            self.delta_threshold_up_usd = self.delta_threshold_usd
            self.delta_threshold_down_usd = self.delta_threshold_usd
        else:
            fallback = self.delta_threshold_usd
            if self.delta_threshold_up_usd is None:
                if fallback is None:
                    raise ValueError("delta_threshold_up_usd required when down is set")
                self.delta_threshold_up_usd = fallback
            if self.delta_threshold_down_usd is None:
                if fallback is None:
                    raise ValueError("delta_threshold_down_usd required when up is set")
                self.delta_threshold_down_usd = fallback
        return self

    def threshold_usd_for_side(self, side: str) -> float:
        if side == "up":
            return float(self.delta_threshold_up_usd or 0.0)
        return float(self.delta_threshold_down_usd or 0.0)

    def format_threshold_usd(self) -> str:
        up = float(self.delta_threshold_up_usd or 0.0)
        down = float(self.delta_threshold_down_usd or 0.0)
        if abs(up - down) < 1e-12:
            return f"${up:.4f}"
        return f"UP ${up:.4f} / DOWN ${down:.4f}"


class StrategyConfig(BaseModel):
    # Buy opportunity detection
    price_feed: Literal["chainlink", "binance"] = "binance"
    lookback_seconds: float = Field(default=30.0, gt=0.0)
    sustain_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Seconds threshold must hold after lookback delta passes (0 = fire immediately)",
    )
    poll_interval_ms: float = Field(default=250.0, ge=50.0, le=2000.0)
    # fixed_usd = per-asset delta_threshold_usd; atr_multiplier = max(fixed, ATR × multiplier)
    threshold_mode: Literal["fixed_usd", "atr_multiplier"] = "fixed_usd"
    atr_period_samples: int = Field(default=30, ge=3, le=200)
    atr_multiplier: float = Field(default=1.5, ge=0.5, le=10.0)
    by_asset: dict[str, AssetStrategyParams] = Field(
        default_factory=lambda: {
            "btc": AssetStrategyParams(delta_threshold_usd=10.0, lookback_seconds=30.0),
            "eth": AssetStrategyParams(delta_threshold_usd=1.0, lookback_seconds=30.0),
            "sol": AssetStrategyParams(delta_threshold_usd=0.05, lookback_seconds=36.0),
            "xrp": AssetStrategyParams(delta_threshold_usd=0.001, lookback_seconds=36.0),
        }
    )
    # Fast order management (limit/market + auto-cancel; increase for slower fills)
    time_limit_cancel_seconds: float = Field(default=4.0, gt=0.0, le=120.0)

    @model_validator(mode="after")
    def _validate_feeds(self) -> StrategyConfig:
        if self.price_feed not in FEED_IDS:
            raise ValueError(f"price_feed must be one of {sorted(FEED_IDS)}")
        return self

    def asset_params(self, asset: str) -> AssetStrategyParams:
        key = asset.lower().strip()
        row = self.by_asset.get(key)
        if row is None:
            raise KeyError(f"strategy.by_asset missing entry for {key!r}")
        lookback = row.lookback_seconds if row.lookback_seconds is not None else self.lookback_seconds
        sustain = row.sustain_seconds if row.sustain_seconds is not None else self.sustain_seconds
        return AssetStrategyParams(
            delta_threshold_usd=row.delta_threshold_usd,
            delta_threshold_up_usd=row.delta_threshold_up_usd,
            delta_threshold_down_usd=row.delta_threshold_down_usd,
            lookback_seconds=lookback,
            sustain_seconds=sustain,
        )


class OrderConfig(BaseModel):
    style: Literal["limit", "market", "market/limit"] = "limit"
    limit_reference: Literal["best_bid", "mid", "best_ask"] = "mid"
    limit_price_offset: float = Field(default=0.0, ge=-0.5, le=0.5)
    limit_price: float | None = Field(default=None, gt=0.0, lt=1.0)
    limit_order_type: Literal["GTC", "FAK", "FOK"] = "GTC"
    market_order_type: Literal["FAK", "FOK"] = "FAK"

    def active_order_type(self, *, as_market: bool) -> str:
        return self.market_order_type if as_market else self.limit_order_type


class TradingConfig(BaseModel):
    position_size: Literal["shares", "usd"] = "shares"
    shares: float = Field(default=5.0, gt=0.0)
    usd: float = Field(default=10.0, gt=0.0)
    order: OrderConfig = Field(default_factory=OrderConfig)


class ExecutionConfig(BaseModel):
    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    private_key: str = ""
    funder: str = ""
    chain_id: int = 137
    signature_type: int = 2
    rpc_url: str = "https://polygon-rpc.com"

    def credentials_ready(self) -> bool:
        return bool(
            self.api_key.strip()
            and self.api_secret.strip()
            and self.api_passphrase.strip()
            and self.private_key.strip()
        )


class RiskConfig(BaseModel):
    enabled: bool = True
    starting_bankroll_usd: float = Field(default=1000.0, gt=0.0)
    max_daily_deployed_usd: float | None = Field(default=200.0, gt=0.0)
    max_daily_drawdown_pct: float | None = Field(default=5.0, gt=0.0, le=100.0)
    max_trades_per_day: int | None = Field(default=50, ge=1, le=500)


class StorageConfig(BaseModel):
    trades_path: str = "data/trades.jsonl"
    status_path: str = "data/bot_status.json"
    trade_snapshots_dir: str = "data/trade_snapshots"
    log_path: str = "data/beat-spike.log"


class FeedsConfig(BaseModel):
    # server = read feeds from dashboard API (run `make server` first).
    # local = embed WS feeds in the bot (bot-only; do not run server concurrently).
    source: Literal["local", "server"] = "server"
    server_url: str = "http://127.0.0.1:8788"
    momentum_window_seconds: float = Field(default=4.0, gt=0.0)


class ChainlinkConfig(BaseModel):
    """Chainlink Data Streams credentials (REST poll + epoch strike lookup)."""

    streams_user_id: str = ""
    streams_secret: str = ""
    latest_poll_sec: float = Field(default=3.0, ge=0.5, le=60.0)
    feed_ids: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_credentials(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("streams_user_id", "streams_secret"):
                if data.get(key) is None:
                    data[key] = ""
        return data

    def ready(self) -> bool:
        return bool(
            self.streams_user_id.strip()
            and self.streams_secret.strip()
            and self.feed_ids
        )


class Settings(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    markets: MarketsConfig = Field(default_factory=MarketsConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    feeds: FeedsConfig = Field(default_factory=FeedsConfig)
    chainlink: ChainlinkConfig = Field(default_factory=ChainlinkConfig)


def _project_root(config_path: Path) -> Path:
    if config_path.parent.name == "config":
        return config_path.parent.parent
    return config_path.parent


def _load_dotenv(root: Path) -> None:
    for base in (root, Path.cwd()):
        env_file = base / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=True)
            return
    load_dotenv(override=True)


def _apply_env_execution(execution: ExecutionConfig) -> None:
    for env_name, attr in _ENV_EXECUTION_KEYS:
        val = os.getenv(env_name, "").strip()
        if val:
            setattr(execution, attr, val)
    for env_name, attr in _LEGACY_EXECUTION_KEYS:
        if getattr(execution, attr):
            continue
        val = os.getenv(env_name, "").strip()
        if val:
            setattr(execution, attr, val)


def _apply_env_bot(settings: Settings) -> None:
    mode = (
        os.getenv("BEAT_SPIKE_BOT__MODE", "").strip().lower()
        or os.getenv("DAWN_BOT__MODE", "").strip().lower()
    )
    if mode in ("paper", "live"):
        settings.bot.mode = mode  # type: ignore[assignment]
    en = (
        os.getenv("BEAT_SPIKE_EXECUTION__ENABLED", "").strip().lower()
        or os.getenv("DAWN_EXECUTION__ENABLED", "").strip().lower()
    )
    if en in ("true", "1", "yes"):
        settings.execution.enabled = True
    elif en in ("false", "0", "no"):
        settings.execution.enabled = False


def _apply_env_chainlink(chainlink: ChainlinkConfig) -> None:
    for env_name, attr in _ENV_CHAINLINK_KEYS:
        val = os.getenv(env_name, "").strip()
        if val:
            setattr(chainlink, attr, val)
    for env_name, attr in _LEGACY_CHAINLINK_KEYS:
        if getattr(chainlink, attr):
            continue
        val = os.getenv(env_name, "").strip()
        if val:
            setattr(chainlink, attr, val)


def load_config(path: str | Path = "config/default.yaml") -> Settings:
    p = Path(path).resolve()
    root = _project_root(p)
    _load_dotenv(root)

    data: dict[str, Any] = {}
    if p.is_file():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    ex = data.get("execution")
    if isinstance(ex, dict):
        ex.pop("private_key", None)
        ex.pop("funder", None)

    settings = Settings(**data)
    _apply_env_execution(settings.execution)
    _apply_env_chainlink(settings.chainlink)
    _apply_env_bot(settings)
    return settings


def validate_for_startup(cfg: Settings) -> None:
    paper = cfg.bot.mode == "paper" or not cfg.execution.enabled
    if paper:
        mode_note = "paper/simulate"
        if cfg.bot.mode == "live" and not cfg.execution.enabled:
            mode_note = "live config but execution.enabled=false → simulate only"
        print(f"[CONFIG] {mode_note}")
        return

    ex = cfg.execution
    missing: list[str] = []
    if not ex.private_key.strip():
        missing.append("BEAT_SPIKE_EXECUTION__PRIVATE_KEY (.env)")
    if not ex.funder.strip():
        missing.append("BEAT_SPIKE_EXECUTION__FUNDER (.env)")
    if not ex.api_key.strip():
        missing.append("api_key (YAML or BEAT_SPIKE_EXECUTION__API_KEY)")
    if not ex.api_secret.strip():
        missing.append("api_secret (YAML or BEAT_SPIKE_EXECUTION__API_SECRET)")
    if not ex.api_passphrase.strip():
        missing.append("api_passphrase (YAML or BEAT_SPIKE_EXECUTION__API_PASSPHRASE)")
    if missing:
        raise SystemExit("[CONFIG] Live CLOB trading requires:\n  - " + "\n  - ".join(missing))
    print("[CONFIG] live CLOB execution enabled (credentials loaded)")
