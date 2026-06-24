"""Persist monitor buy events and chart snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.history_store import chart_has_series_data


def monitor_chart_id(ts_unix: float, slug: str, tx_hash: str) -> str:
    ts_iso = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    raw = f"{slug.strip().lower()}_{ts_iso}_{tx_hash[-8:]}"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)
    return safe[:180]


@dataclass
class MonitorEventRecord:
    ts: str
    target: str
    target_url: str
    proxy_wallet: str
    slug: str
    asset: str
    interval: str
    side: str
    price: float
    size: float
    usdc_size: float
    outcome: str
    transaction_hash: str
    chart_id: str = ""


class MonitorStore:
    def __init__(self, events_path: Path, snapshots_dir: Path, state_path: Path) -> None:
        self.events_path = events_path
        self.snapshots_dir = snapshots_dir
        self.state_path = state_path
        events_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._seen_tx: set[str] = set()
        self._load_state()

    def _load_state(self) -> None:
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                txs = data.get("seen_transactions")
                if isinstance(txs, list):
                    self._seen_tx = {str(t).lower() for t in txs}
            except (OSError, json.JSONDecodeError):
                pass
        if not self._seen_tx and self.events_path.is_file():
            for line in self.events_path.read_text(encoding="utf-8").strip().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tx = str(row.get("transaction_hash") or "").lower()
                if tx:
                    self._seen_tx.add(tx)

    def _save_state(self) -> None:
        payload = {"seen_transactions": sorted(self._seen_tx)[-5000:]}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def has_transaction(self, tx_hash: str) -> bool:
        return tx_hash.lower() in self._seen_tx

    def mark_transaction(self, tx_hash: str) -> None:
        self._seen_tx.add(tx_hash.lower())
        self._save_state()

    def save_snapshot(self, chart_id: str, payload: dict[str, Any]) -> Path:
        path = self.snapshots_dir / f"{chart_id}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return path

    def append_event(self, record: MonitorEventRecord, *, chart: dict[str, Any] | None = None) -> None:
        row = asdict(record)
        if chart is not None and chart_has_series_data(chart):
            cid = record.chart_id or monitor_chart_id(0, record.slug, record.transaction_hash)
            row["chart_id"] = cid
            self.save_snapshot(cid, chart)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.mark_transaction(record.transaction_hash)

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        lines = self.events_path.read_text(encoding="utf-8").strip().splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))
