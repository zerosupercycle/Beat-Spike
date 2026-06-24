from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILL_STATUSES = frozenset({"paper_filled", "simulated", "matched", "live", "filled", "success"})


def resolve_storage_path(path: str, project_root: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else project_root / p


def is_filled_status(status: str) -> bool:
    s = status.lower().strip()
    if s in FILL_STATUSES:
        return True
    return "fill" in s or "match" in s


def chart_has_series_data(chart: dict[str, Any] | None) -> bool:
    if not chart or chart.get("error"):
        return False
    series = chart.get("series")
    if not isinstance(series, dict):
        return False
    return any(isinstance(v, list) and len(v) > 0 for v in series.values())


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class TradeRecord:
    """One JSONL line in storage.trades_path (append-only, one object per line)."""

    ts: str
    mode: str
    asset: str
    interval: str
    slug: str
    side: str
    token_id: str
    price: float
    shares: float
    size_usd: float
    position_size_mode: str
    best_ask: float
    feed_price: float
    price_delta_usd: float
    signal_feed: str
    order_style: str
    order_type: str
    status: str
    decision: str = "pass"
    detail: str = ""
    chart_id: str = ""


def trade_chart_id(ts: str, slug: str) -> str:
    raw = f"{slug.strip().lower()}_{ts.strip()}"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)
    return safe[:180]


class HistoryStore:
    def __init__(self, trades_path: Path, status_path: Path, snapshots_dir: Path) -> None:
        self.trades_path = trades_path
        self.status_path = status_path
        self.snapshots_dir = snapshots_dir
        trades_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)

    def save_chart_snapshot(self, chart_id: str, payload: dict[str, Any]) -> Path:
        path = self.snapshots_dir / f"{chart_id}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return path

    def append_trade(self, record: TradeRecord, *, chart: dict[str, Any] | None = None) -> None:
        row = asdict(record)
        if chart is not None and chart_has_series_data(chart):
            cid = record.chart_id or trade_chart_id(record.ts, record.slug)
            row["chart_id"] = cid
            self.save_chart_snapshot(cid, chart)
        with self.trades_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def list_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.trades_path.is_file():
            return []
        lines = self.trades_path.read_text(encoding="utf-8").strip().splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))

    def write_status(self, status: dict[str, Any]) -> None:
        status["updated_at"] = _utc_iso()
        self.status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")

    def read_status(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            return {"state": "idle"}
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"state": "error", "detail": "invalid status file"}
