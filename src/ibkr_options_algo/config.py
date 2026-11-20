"""Typed, validated settings loaded from config.toml."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ibkr_options_algo.errors import ConfigError
from ibkr_options_algo.models import Strategy

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

#: Live-trading ports. Never valid in config: the algo only talks to paper sessions.
LIVE_PORTS: frozenset[int] = frozenset({7496, 4001})

DEFAULT_CONFIG_PATH = Path("config.toml")

Percent = float


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientIds(_Section):
    check: int = Field(ge=0)
    chain: int = Field(ge=0)
    screen: int = Field(ge=0)
    pick: int = Field(ge=0)
    orders: int = Field(ge=0)
    report: int = Field(ge=0)


class IbkrConfig(_Section):
    host: str = Field(min_length=1)
    paper_ports: tuple[int, ...] = Field(min_length=1)
    connect_timeout_seconds: float = Field(gt=0)
    market_data_type: Literal[1, 2, 3, 4]
    quote_batch_size: int = Field(gt=0)
    quote_wait_seconds: float = Field(ge=0)
    greeks_timeout_seconds: int = Field(ge=1)
    pnl_wait_seconds: float = Field(ge=0)
    order_ack_wait_seconds: float = Field(ge=0)
    history_duration: str = Field(min_length=1)
    history_timeout_seconds: float = Field(gt=0)
    client_ids: ClientIds

    @field_validator("paper_ports")
    @classmethod
    def _no_live_ports(cls, ports: tuple[int, ...]) -> tuple[int, ...]:
        live = sorted(set(ports) & LIVE_PORTS)
        if live:
            raise ValueError(f"live trading port(s) {live} are not allowed; use paper ports only")
        return ports


class ScreenConfig(_Section):
    num_stocks: int = Field(gt=0, le=50)
    include_etfs: bool
    scan_code: str = Field(min_length=1)
    location_code: str = Field(min_length=1)
    min_iv_rank: float = Field(ge=0, le=100)
    min_iv_hv_gap: float
    hv_days: int = Field(ge=2)
    min_iv_history_days: int = Field(ge=1)


class PickConfig(_Section):
    min_dte: int = Field(ge=1)
    max_dte: int = Field(ge=1)
    target_dte: int = Field(ge=1)
    fallback_min_dte: int = Field(ge=0)
    put_delta: float = Field(gt=0, lt=1)
    call_delta: float = Field(gt=0, lt=1)
    strangle_delta: float = Field(gt=0, lt=1)
    delta_tolerance: float = Field(gt=0, lt=1)
    earnings_buffer_days: int = Field(ge=0)
    max_strikes_per_side: int = Field(gt=0)
    strike_range_expected_moves: float = Field(gt=0)
    default_iv: float = Field(gt=0)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> PickConfig:
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError("need min_dte <= target_dte <= max_dte")
        return self


class RiskConfig(_Section):
    max_trade_pct: Percent = Field(gt=0, le=100)
    max_total_pct: Percent = Field(gt=0, le=100)
    max_positions: int = Field(gt=0)
    max_spread_pct: Percent = Field(gt=0)
    large_cap_usd: float = Field(gt=0)
    earnings_buffer_days: int = Field(ge=0)
    block_earnings_before_expiry: bool
    strategy_priority: tuple[Strategy, ...] = Field(min_length=1)

    @field_validator("strategy_priority")
    @classmethod
    def _unique(cls, value: tuple[Strategy, ...]) -> tuple[Strategy, ...]:
        if len(set(value)) != len(value):
            raise ValueError("strategy_priority lists a strategy twice")
        return value

    @model_validator(mode="after")
    def _trade_within_total(self) -> RiskConfig:
        if self.max_trade_pct > self.max_total_pct:
            raise ValueError("max_trade_pct cannot exceed max_total_pct")
        return self


class ReportConfig(_Section):
    take_profit_pct: Percent = Field(gt=0, le=100)
    close_at_dte: int = Field(ge=0)


class EarningsConfig(_Section):
    days_ahead: int = Field(ge=1)
    request_delay_seconds: float = Field(ge=0)
    max_failed_days_to_cache: int = Field(ge=0)
    abort_after_consecutive_failures: int = Field(ge=1)


class NetworkConfig(_Section):
    timeout_seconds: float = Field(gt=0)
    retries: int = Field(ge=0)
    backoff_seconds: float = Field(ge=0)
    user_agent: str = Field(min_length=1)


class ChainConfig(_Section):
    min_dte: int = Field(ge=0)
    max_dte: int = Field(ge=0)
    strike_range_pct: Percent = Field(gt=0, lt=100)
    fallback_expirations: int = Field(ge=1)
    preview_rows: int = Field(ge=0)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> ChainConfig:
        if self.min_dte > self.max_dte:
            raise ValueError("need min_dte <= max_dte")
        return self


class OutputConfig(_Section):
    directory: Path


class AppConfig(_Section):
    """All settings. Mirrors the sections of config.toml one to one."""

    ibkr: IbkrConfig
    screen: ScreenConfig
    pick: PickConfig
    risk: RiskConfig
    report: ReportConfig
    earnings: EarningsConfig
    network: NetworkConfig
    chain: ChainConfig
    output: OutputConfig


def parse_config(data: dict[str, object]) -> AppConfig:
    """Validate an already-parsed TOML mapping."""
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"config.toml is invalid:\n{problems}") from exc


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Read and validate a config file. Raises ConfigError with a readable message."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config file {path} not found. Run from the repo folder or pass --config PATH."
        ) from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    return parse_config(data)
