import type { UTCTimestamp } from 'lightweight-charts';
import { CHART_HISTORY_SEC } from './chartTimeRange';
import type { DashboardSnapshot, Momentum } from './types';

export type HistoryPoint = {
  time: UTCTimestamp;
  price: number;
  momentum: number;
  direction: 'up' | 'down' | 'neutral';
};

export type SeriesKey = string; // `${feedId}:${asset}`

const HISTORY_SEC = CHART_HISTORY_SEC;
const MAX_POINTS = 500;
const APPEND_INTERVAL_MS = 200;

export function momentumScalar(mom: Momentum | null | undefined): number {
  if (!mom) return 0;
  const roc = mom.roc_percent?.value;
  if (roc != null && Number.isFinite(roc)) return roc;
  const abs = mom.absolute?.value;
  if (abs != null && Number.isFinite(abs)) return abs;
  return 0;
}

export function formatMomentumDisplay(mom: Momentum | null | undefined): {
  text: string;
  label: string;
  direction: Momentum['aggregate_direction'] | undefined;
} {
  if (!mom) return { text: '—', label: 'ROC', direction: undefined };

  const roc = mom.roc_percent?.value;
  if (roc != null && Number.isFinite(roc)) {
    const sign = roc > 0 ? '+' : roc < 0 ? '' : '';
    return { text: `${sign}${roc.toFixed(4)}%`, label: 'ROC', direction: mom.aggregate_direction };
  }

  const abs = mom.absolute?.value;
  if (abs != null && Number.isFinite(abs)) {
    const sign = abs > 0 ? '+' : abs < 0 ? '' : '';
    return { text: `${sign}${abs.toFixed(2)}`, label: 'Δ', direction: mom.aggregate_direction };
  }

  return { text: '—', label: 'ROC', direction: mom.aggregate_direction };
}

function nextChartTime(prev: HistoryPoint | undefined): UTCTimestamp {
  let t = Date.now() / 1000;
  if (prev && t <= prev.time) {
    t = prev.time + 0.001;
  }
  return t as UTCTimestamp;
}

export type HistoryStore = Map<SeriesKey, HistoryPoint[]>;

export function ingestSnapshot(store: HistoryStore, snap: DashboardSnapshot): HistoryStore {
  const nowMs = Date.now();
  const cutoff = (nowMs / 1000 - HISTORY_SEC) as UTCTimestamp;

  for (const [feedId, feed] of Object.entries(snap.feeds)) {
    for (const [asset, row] of Object.entries(feed.assets)) {
      if (row.price == null || !Number.isFinite(row.price)) continue;
      const key: SeriesKey = `${feedId}:${asset}`;
      const prev = store.get(key) ?? [];
      const last = prev[prev.length - 1];

      const nextPoint: HistoryPoint = {
        time: last ? last.time : nextChartTime(undefined),
        price: row.price,
        momentum: momentumScalar(row.momentum),
        direction: row.momentum?.aggregate_direction ?? 'neutral',
      };

      let series: HistoryPoint[];
      const lastMs = last ? (last.time as number) * 1000 : 0;

      if (!last) {
        nextPoint.time = nextChartTime(undefined);
        series = [nextPoint];
      } else if (nowMs - lastMs < APPEND_INTERVAL_MS) {
        series = [...prev.slice(0, -1), nextPoint];
      } else {
        nextPoint.time = nextChartTime(last);
        series = [...prev, nextPoint];
      }

      store.set(key, series.filter((p) => p.time >= cutoff).slice(-MAX_POINTS));
    }
  }

  return store;
}

export { FEED_COLORS } from './feeds';
