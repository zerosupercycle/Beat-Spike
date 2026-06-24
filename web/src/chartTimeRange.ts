import type { IChartApi, UTCTimestamp } from 'lightweight-charts';

/** Smallest visible window when zoomed in (5 minutes). */
export const MIN_VISIBLE_RANGE_SEC = 5 * 60;

/** Keep at least this much history so zoom-out has context beyond 5m. */
export const CHART_HISTORY_SEC = 10 * 60;

export const CHART_INTERACTION_HINT =
  'Mouse wheel: zoom time (5m min) · drag: pan · double-click axis: reset';

/** Smallest visible window when zooming into an order snapshot. */
export const SNAPSHOT_MIN_VISIBLE_RANGE_SEC = 30;

export const SNAPSHOT_CHART_INTERACTION_HINT =
  'Mouse wheel: zoom time · drag: pan · double-click time axis: reset';

export function clampVisibleTimeRange(chart: IChartApi, minSpanSec: number = MIN_VISIBLE_RANGE_SEC): void {
  const ts = chart.timeScale();
  const range = ts.getVisibleRange();
  if (!range) return;

  const from = range.from as number;
  const to = range.to as number;
  const span = to - from;
  if (!Number.isFinite(span) || span >= minSpanSec) return;

  const mid = (from + to) / 2;
  const half = minSpanSec / 2;
  ts.setVisibleRange({
    from: (mid - half) as UTCTimestamp,
    to: (mid + half) as UTCTimestamp,
  });
}
