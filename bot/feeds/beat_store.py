"""Persist per-slug feed beat prices snapped at market epoch open."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _PROJECT_ROOT / "data" / "feed_beats.json"


class FeedBeatStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = data.get("slugs") if isinstance(data, dict) else None
        if isinstance(rows, dict):
            self._rows = {str(k).lower(): v for k, v in rows.items() if isinstance(v, dict)}

    def _save(self) -> None:
        payload = {"slugs": self._rows}
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, slug: str) -> dict[str, Any] | None:
        return self._rows.get(slug.strip().lower())

    def get_feed_beats(self, slug: str) -> dict[str, float]:
        row = self.get(slug)
        if not row:
            return {}
        raw = row.get("feed_beats")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for fid, px in raw.items():
            if px is None:
                continue
            try:
                fv = float(px)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                out[str(fid)] = fv
        return out

    def all_rows(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows)

    def save(
        self,
        slug: str,
        *,
        epoch_start: int,
        feed_beats: dict[str, float | None],
        merge: bool = True,
    ) -> None:
        slug_key = slug.strip().lower()
        cleaned: dict[str, float] = {}
        for fid, px in feed_beats.items():
            if px is None:
                continue
            try:
                fv = float(px)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                cleaned[str(fid)] = fv
        if not cleaned:
            return
        prev = self._rows.get(slug_key, {})
        prev_beats = dict(prev.get("feed_beats") or {}) if merge else {}
        for fid, px in cleaned.items():
            prev_beats[fid] = px
        self._rows[slug_key] = {
            "slug": slug_key,
            "epoch_start": int(epoch_start),
            "feed_beats": prev_beats,
        }
        self._save()
