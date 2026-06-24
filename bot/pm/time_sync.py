from __future__ import annotations

import json
import statistics
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class ServerTimeSyncResult:
    offset_sec: float
    samples: int
    kept: int
    best_rtt_ms: float = 0.0
    worst_kept_ms: float = 0.0
    synced_server_time_iso: str = ""
    synced_unix: float = 0.0


def _iso_z_trunc_ms(unix_ts: float) -> str:
    dt = datetime.fromtimestamp(float(unix_ts), tz=UTC)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def print_server_time_sync_line(result: ServerTimeSyncResult) -> None:
    clock = datetime.now(tz=UTC)
    head = clock.strftime("%Y-%m-%dT%H:%M:%S") + f".{clock.microsecond // 1000:03d}Z"
    print(
        f"{head}  INFO time sync samples={result.samples} kept={result.kept} "
        f"best_rtt_ms={int(round(result.best_rtt_ms))} worst_kept_ms={int(round(result.worst_kept_ms))} "
        f"offset_s={result.offset_sec} synced_server_time={result.synced_server_time_iso} "
        f"synced_unix={result.synced_unix}"
    )


def measure_server_time_offset_sync(
    clob_url: str,
    *,
    n_samples: int = 20,
    n_keep: int = 10,
    timeout: float = 10.0,
) -> ServerTimeSyncResult:
    url = clob_url.rstrip("/") + "/time"
    headers = {
        "User-Agent": "py_clob_client_v2",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    n_samples = max(1, int(n_samples))
    n_keep = max(1, min(int(n_keep), n_samples))
    raw: list[tuple[float, float]] = []

    for _ in range(n_samples):
        t_start = time.time()
        try:
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode().strip()
            t_end = time.time()
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    server_ts = float(parsed.get("timestamp") or parsed.get("time") or 0)
                else:
                    server_ts = float(parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                server_ts = float(body)
            if server_ts > 1e12:
                server_ts /= 1000.0
            if server_ts <= 0:
                continue
            rtt = t_end - t_start
            offset = (server_ts + rtt / 2.0) - t_end
            raw.append((rtt, offset))
        except Exception:
            continue

    if not raw:
        return ServerTimeSyncResult(0.0, n_samples, 0)

    raw.sort(key=lambda x: x[0])
    kept_pairs = raw[:n_keep]
    offsets_kept = [p[1] for p in kept_pairs]
    rtts_kept = [p[0] for p in kept_pairs]
    offset_mean = float(statistics.mean(offsets_kept))
    t_done = time.time()
    synced_unix = t_done + offset_mean
    return ServerTimeSyncResult(
        offset_sec=offset_mean,
        samples=n_samples,
        kept=len(kept_pairs),
        best_rtt_ms=rtts_kept[0] * 1000.0,
        worst_kept_ms=rtts_kept[-1] * 1000.0,
        synced_server_time_iso=_iso_z_trunc_ms(synced_unix),
        synced_unix=synced_unix,
    )
