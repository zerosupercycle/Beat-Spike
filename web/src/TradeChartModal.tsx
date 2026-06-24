import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ColorType,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  LineSeries,
  LineStyle,
  LineType,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type AutoscaleInfo,
  type LineData,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts';
import {
  clampVisibleTimeRange,
  SNAPSHOT_CHART_INTERACTION_HINT,
  SNAPSHOT_MIN_VISIBLE_RANGE_SEC,
} from './chartTimeRange';
import { FEED_COLORS, feedDisplayLabel } from './feeds';
import { INTERVAL_SECONDS, parseMarketSlug, slugEpochBounds } from './slugNav';
import { formatEtDateTime, formatEtTick, formatEtTime } from './timeEt';
import type { TradeRow } from './TradeHistory';

type SnapshotPoint = { t: number; price: number; momentum: number };

type TradeChartPayload = {
  captured_at?: string;
  asset?: string;
  slug?: string;
  interval?: string;
  source?: string;
  epoch_start?: number;
  epoch_end?: number;
  window_start?: number;
  window_end?: number;
  window_before_sec?: number;
  window_after_sec?: number;
  order_ts?: number;
  beat_price?: number | null;
  polymarket_beat?: number | null;
  feed_beats?: Record<string, number>;
  finalized_at?: string;
  window_sec?: number;
  series?: Record<string, SnapshotPoint[]>;
  monitor?: {
    target?: string;
    target_url?: string;
    slug?: string;
    outcome?: string;
  };
  trade?: {
    ts?: string;
    side?: string;
    price?: number;
    avg_roc?: number;
    shares?: number;
  };
  error?: string;
};

type ChartContext = {
  beatPrice: number | null;
  orderTs: number | null;
  epochStart: number;
  epochEnd: number;
};

type Props = {
  trade: TradeRow;
  onClose: () => void;
  variant?: 'bot' | 'monitor';
  title?: string;
};

function seriesMaxTime(payload: TradeChartPayload | null): number {
  if (!payload?.series) return 0;
  let maxT = 0;
  for (const pts of Object.values(payload.series)) {
    if (pts.length > 0) maxT = Math.max(maxT, pts[pts.length - 1].t);
  }
  return maxT;
}

function snapshotNeedsMoreData(payload: TradeChartPayload | null): boolean {
  if (!payload || payload.error || payload.finalized_at) return false;
  if (payload.source === 'monitor') {
    const windowEnd = payload.window_end;
    if (windowEnd == null || !Number.isFinite(windowEnd)) return false;
    return seriesMaxTime(payload) < windowEnd - 0.5;
  }
  const epochEnd = payload.epoch_end;
  if (epochEnd == null || !Number.isFinite(epochEnd)) return false;
  return seriesMaxTime(payload) < epochEnd - 0.5;
}

async function fetchTradeChart(
  chartId: string,
  variant: 'bot' | 'monitor' = 'bot',
): Promise<TradeChartPayload> {
  const base =
    variant === 'monitor' ? '/api/monitor/trade-chart' : '/api/bot/trade-chart';
  const res = await fetch(`${base}/${encodeURIComponent(chartId)}`);
  const data = (await res.json()) as TradeChartPayload & { detail?: string };
  if (!res.ok) {
    return { error: data.error ?? data.detail ?? 'not found' };
  }
  if (data.detail && !data.series) {
    return { error: data.detail };
  }
  return data;
}

function withMinPoints(data: LineData<UTCTimestamp>[]): LineData<UTCTimestamp>[] {
  if (data.length >= 2) return data;
  if (data.length === 0) return data;
  const p = data[0];
  return [
    { time: (p.time - 0.001) as UTCTimestamp, value: p.value },
    p,
  ];
}

function toLineData(points: SnapshotPoint[], pick: 'price' | 'momentum'): LineData<UTCTimestamp>[] {
  const out: LineData<UTCTimestamp>[] = [];
  for (const p of points) {
    const time = p.t as UTCTimestamp;
    const value = pick === 'price' ? p.price : p.momentum;
    const last = out[out.length - 1];
    if (last && last.time === time) {
      last.value = value;
      continue;
    }
    if (!last || time > last.time) {
      out.push({ time, value });
    }
  }
  return withMinPoints(out);
}

function resolveFeedBeat(
  payload: TradeChartPayload | null,
  feedId: string,
  asset: string,
): number | null {
  const key = `${feedId}:${asset.toUpperCase()}`;
  const fromMap = payload?.feed_beats?.[key];
  if (fromMap != null && Number.isFinite(Number(fromMap))) return Number(fromMap);
  return null;
}

function resolveChartContext(
  trade: TradeRow,
  payload: TradeChartPayload | null,
  feedId?: string,
): ChartContext {
  const bounds = slugEpochBounds(trade.slug);
  const parsed = parseMarketSlug(trade.slug);
  let epochStart = payload?.epoch_start ?? bounds?.start ?? parsed?.epochTs ?? 0;
  const interval = payload?.interval ?? parsed?.interval ?? '5m';
  const intervalSec = INTERVAL_SECONDS[interval] ?? 300;
  let epochEnd = payload?.epoch_end ?? bounds?.end ?? (epochStart ? epochStart + intervalSec : 0);
  if (payload?.source === 'monitor') {
    if (payload.window_start != null) epochStart = Number(payload.window_start);
    if (payload.window_end != null) epochEnd = Number(payload.window_end);
  }

  let orderTs: number | null = payload?.order_ts ?? null;
  if (orderTs == null) {
    const ms = Date.parse(trade.ts);
    if (Number.isFinite(ms)) orderTs = ms / 1000;
  }

  const beatRaw =
    (feedId ? resolveFeedBeat(payload, feedId, trade.asset) : null) ??
    payload?.feed_beats?.[`${trade.signal_feed ?? ''}:${trade.asset.toUpperCase()}`] ??
    payload?.beat_price;
  const beatPrice =
    beatRaw != null && Number.isFinite(Number(beatRaw)) ? Number(beatRaw) : null;

  return { beatPrice, orderTs, epochStart, epochEnd };
}

function nearestMarkerTime(points: SnapshotPoint[], target: number): UTCTimestamp {
  if (points.length === 0) return target as UTCTimestamp;
  let best = points[0].t;
  let bestD = Math.abs(points[0].t - target);
  for (const p of points) {
    const d = Math.abs(p.t - target);
    if (d < bestD) {
      bestD = d;
      best = p.t;
    }
  }
  return best as UTCTimestamp;
}

function priceAtTime(points: SnapshotPoint[], target: number): number | null {
  if (points.length === 0) return null;
  let best = points[0];
  let bestD = Math.abs(points[0].t - target);
  for (const p of points) {
    const d = Math.abs(p.t - target);
    if (d < bestD) {
      bestD = d;
      best = p;
    }
  }
  return best.price;
}

function formatPrice(n: number, asset: string): string {
  const digits = asset.toUpperCase() === 'XRP' ? 4 : 2;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function momPriceFormat() {
  return {
    type: 'custom' as const,
    minMove: 0.0001,
    formatter: (p: number) => {
      if (!Number.isFinite(p)) return '—';
      const sign = p > 0 ? '+' : '';
      return `${sign}${p.toFixed(4)}%`;
    },
  };
}

function pricePriceFormat(asset: string) {
  const prec = asset.toUpperCase() === 'XRP' ? 4 : 2;
  return {
    type: 'price' as const,
    precision: prec,
    minMove: asset.toUpperCase() === 'XRP' ? 0.0001 : 0.01,
  };
}

function beatAutoscaleProvider(beatPrice: number | null | undefined) {
  return (original: () => AutoscaleInfo | null) => {
    const res = original();
    if (res === null || res.priceRange === null || beatPrice == null || !Number.isFinite(beatPrice)) {
      return res;
    }
    const min = Math.min(res.priceRange.minValue, beatPrice);
    const max = Math.max(res.priceRange.maxValue, beatPrice);
    const span = max - min;
    const pad = span > 0 ? span * 0.04 : Math.max(Math.abs(beatPrice) * 0.001, 1);
    return {
      priceRange: {
        minValue: min - pad,
        maxValue: max + pad,
      },
    };
  };
}

function applyBeatAutoscale(
  priceSeries: ISeriesApi<'Line'> | null,
  beatPrice: number | null | undefined,
) {
  if (!priceSeries) return;
  priceSeries.applyOptions({
    autoscaleInfoProvider: beatAutoscaleProvider(beatPrice),
  });
}

function SnapshotCharts({
  feedId,
  asset,
  points,
  ctx,
}: {
  feedId: string;
  asset: string;
  points: SnapshotPoint[];
  ctx: ChartContext;
}) {
  const priceRef = useRef<HTMLDivElement>(null);
  const momRef = useRef<HTMLDivElement>(null);
  const priceChartRef = useRef<IChartApi | null>(null);
  const momChartRef = useRef<IChartApi | null>(null);
  const priceSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const momSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const beatLineRef = useRef<IPriceLine | null>(null);
  const markersPluginRef = useRef<{ detach: () => void } | null>(null);
  const rangeSyncSourceRef = useRef<'none' | 'mom' | 'price'>('none');
  const programmaticRangeRef = useRef(false);
  const initialRangeSetRef = useRef(false);
  const lastDataSigRef = useRef('');
  const pointsRef = useRef(points);
  const epochRef = useRef({ start: ctx.epochStart, end: ctx.epochEnd });
  const beatPriceRef = useRef(ctx.beatPrice);
  const orderTsRef = useRef(ctx.orderTs);
  pointsRef.current = points;
  epochRef.current = { start: ctx.epochStart, end: ctx.epochEnd };
  beatPriceRef.current = ctx.beatPrice;
  orderTsRef.current = ctx.orderTs;
  const color = FEED_COLORS[feedId] ?? '#94a3b8';
  const [renderError, setRenderError] = useState<string | null>(null);

  const syncTimeFromMom = () => {
    const momChart = momChartRef.current;
    const priceChart = priceChartRef.current;
    if (!momChart || !priceChart) return;
    rangeSyncSourceRef.current = 'mom';
    const range = momChart.timeScale().getVisibleLogicalRange();
    if (range) priceChart.timeScale().setVisibleLogicalRange(range);
    rangeSyncSourceRef.current = 'none';
  };

  const syncTimeFromPrice = () => {
    const momChart = momChartRef.current;
    const priceChart = priceChartRef.current;
    if (!momChart || !priceChart) return;
    rangeSyncSourceRef.current = 'price';
    const range = priceChart.timeScale().getVisibleLogicalRange();
    if (range) momChart.timeScale().setVisibleLogicalRange(range);
    rangeSyncSourceRef.current = 'none';
  };

  const onMomVisibleRangeChange = () => {
    const momChart = momChartRef.current;
    if (!momChart || rangeSyncSourceRef.current === 'price') return;
    if (!programmaticRangeRef.current) {
      clampVisibleTimeRange(momChart, SNAPSHOT_MIN_VISIBLE_RANGE_SEC);
    }
    syncTimeFromMom();
  };

  const onPriceVisibleRangeChange = () => {
    const momChart = momChartRef.current;
    const priceChart = priceChartRef.current;
    if (!momChart || !priceChart || rangeSyncSourceRef.current === 'mom') return;
    if (!programmaticRangeRef.current) {
      syncTimeFromPrice();
      clampVisibleTimeRange(momChart, SNAPSHOT_MIN_VISIBLE_RANGE_SEC);
      syncTimeFromMom();
    }
  };

  const applySnapshotData = () => {
    const priceSeries = priceSeriesRef.current;
    const momSeries = momSeriesRef.current;
    if (!priceSeries || !momSeries) return;
    setRenderError(null);

    try {
      const pts = pointsRef.current;
      const last = pts[pts.length - 1];
      const sig = `${pts.length}|${last?.t ?? 0}|${last?.price ?? 0}`;
      if (sig === lastDataSigRef.current) {
        return;
      }
      lastDataSigRef.current = sig;
      priceSeries.setData(toLineData(pts, 'price'));
      momSeries.setData(toLineData(pts, 'momentum'));
      applyBeatAutoscale(priceSeries, beatPriceRef.current);
      const { start, end } = epochRef.current;
      applyInitialRange(start, end);
    } catch (err) {
      setRenderError(err instanceof Error ? err.message : 'Failed to render chart');
    }
  };

  const applySnapshotAnnotations = () => {
    const priceSeries = priceSeriesRef.current;
    if (!priceSeries) return;

    markersPluginRef.current?.detach();
    markersPluginRef.current = null;

    if (beatLineRef.current) {
      priceSeries.removePriceLine(beatLineRef.current);
      beatLineRef.current = null;
    }

    const beatPrice = beatPriceRef.current;
    applyBeatAutoscale(priceSeries, beatPrice);
    if (beatPrice != null && Number.isFinite(beatPrice)) {
      beatLineRef.current = priceSeries.createPriceLine({
        price: beatPrice,
        color,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `${feedDisplayLabel(feedId)} beat`,
      });
    }

    const orderTs = orderTsRef.current;
    const pts = pointsRef.current;
    if (orderTs != null && pts.length > 0) {
      const markerTime = nearestMarkerTime(pts, orderTs);
      const markerPrice =
        priceAtTime(pts, orderTs) ??
        pts[pts.length - 1]?.price ??
        beatPrice ??
        0;
      const markers: SeriesMarker<UTCTimestamp>[] = [
        {
          time: markerTime,
          position: 'atPriceMiddle',
          price: markerPrice,
          color: '#38bdf8',
          shape: 'arrowDown',
          text: 'Buy',
        },
      ];
      markersPluginRef.current = createSeriesMarkers(priceSeries, markers);
    }

    // Force price scale to include beat line in visible range.
    const ptsForScale = pointsRef.current;
    if (ptsForScale.length > 0) {
      priceSeries.setData(toLineData(ptsForScale, 'price'));
    }
  };

  const applyInitialRange = (epochStart: number, epochEnd: number) => {
    const momChart = momChartRef.current;
    const priceChart = priceChartRef.current;
    if (!momChart || !priceChart || initialRangeSetRef.current) return;
    initialRangeSetRef.current = true;
    programmaticRangeRef.current = true;
    try {
      if (epochStart > 0 && epochEnd > epochStart) {
        const range = {
          from: epochStart as UTCTimestamp,
          to: epochEnd as UTCTimestamp,
        };
        momChart.timeScale().setVisibleRange(range);
        priceChart.timeScale().setVisibleRange(range);
      } else {
        momChart.timeScale().fitContent();
        priceChart.timeScale().fitContent();
      }
      syncTimeFromMom();
    } finally {
      programmaticRangeRef.current = false;
    }
  };

  useEffect(() => {
    const priceEl = priceRef.current;
    const momEl = momRef.current;
    if (!priceEl || !momEl) return;
    setRenderError(null);
    initialRangeSetRef.current = false;

    let disposed = false;

    const mount = () => {
      if (disposed || priceChartRef.current) return;
      const pw = priceEl.clientWidth;
      const ph = priceEl.clientHeight;
      const mw = momEl.clientWidth;
      const mh = momEl.clientHeight;
      if (pw < 8 || ph < 8 || mw < 8 || mh < 8) return;

      const baseLayout = {
        layout: {
          background: { type: ColorType.Solid, color: 'transparent' },
          textColor: '#6b7589',
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: 10,
          attributionLogo: false,
        },
        grid: {
          vertLines: { visible: false },
          horzLines: { color: 'rgba(37, 43, 54, 0.35)' },
        },
        crosshair: { mode: CrosshairMode.Magnet, vertLine: { visible: false }, horzLine: { visible: false } },
        leftPriceScale: { visible: false },
        handleScroll: {
          mouseWheel: false,
          pressedMouseMove: false,
          horzTouchDrag: false,
        },
        handleScale: {
          mouseWheel: false,
          pinch: false,
          axisPressedMouseMove: { time: false, price: false },
        },
      } as const;

      const interaction = {
        handleScroll: {
          mouseWheel: false,
          pressedMouseMove: true,
          horzTouchDrag: true,
        },
        handleScale: {
          mouseWheel: true,
          pinch: true,
          axisPressedMouseMove: { time: true, price: false },
          axisDoubleClickReset: { time: true, price: false },
        },
      };

      const priceChart = createChart(priceEl, {
        ...baseLayout,
        ...interaction,
        width: pw,
        height: ph,
        timeScale: { visible: false, borderVisible: false },
        rightPriceScale: { visible: true, borderVisible: false, minimumWidth: 64 },
      });
      const momChart = createChart(momEl, {
        ...baseLayout,
        ...interaction,
        width: mw,
        height: mh,
        localization: { timeFormatter: formatEtTime },
        timeScale: {
          visible: true,
          borderVisible: false,
          timeVisible: true,
          secondsVisible: true,
          tickMarkFormatter: formatEtTick,
          fixLeftEdge: false,
          fixRightEdge: false,
        },
        rightPriceScale: { visible: true, borderVisible: false, minimumWidth: 64 },
      });

      priceChartRef.current = priceChart;
      momChartRef.current = momChart;
      priceSeriesRef.current = priceChart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        lineType: LineType.Curved,
        priceFormat: pricePriceFormat(asset),
        autoscaleInfoProvider: beatAutoscaleProvider(beatPriceRef.current),
      });
      momSeriesRef.current = momChart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        lineType: LineType.Curved,
        priceFormat: momPriceFormat(),
      });

      momChart.timeScale().subscribeVisibleLogicalRangeChange(onMomVisibleRangeChange);
      priceChart.timeScale().subscribeVisibleLogicalRangeChange(onPriceVisibleRangeChange);
      applySnapshotData();
      applySnapshotAnnotations();
    };

    const ro = new ResizeObserver(() => {
      if (disposed) return;
      if (!priceChartRef.current) {
        mount();
        return;
      }
      const w1 = priceEl.clientWidth;
      const h1 = priceEl.clientHeight;
      const w2 = momEl.clientWidth;
      const h2 = momEl.clientHeight;
      if (w1 > 0 && h1 > 0) priceChartRef.current.applyOptions({ width: w1, height: h1 });
      if (w2 > 0 && h2 > 0) momChartRef.current?.applyOptions({ width: w2, height: h2 });
      syncTimeFromMom();
    });
    ro.observe(priceEl);
    ro.observe(momEl);
    requestAnimationFrame(mount);

    return () => {
      disposed = true;
      ro.disconnect();
      markersPluginRef.current?.detach();
      markersPluginRef.current = null;
      if (beatLineRef.current && priceSeriesRef.current) {
        priceSeriesRef.current.removePriceLine(beatLineRef.current);
        beatLineRef.current = null;
      }
      priceChartRef.current?.remove();
      momChartRef.current?.remove();
      priceChartRef.current = null;
      momChartRef.current = null;
      priceSeriesRef.current = null;
      momSeriesRef.current = null;
      initialRangeSetRef.current = false;
      lastDataSigRef.current = '';
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feedId, asset, color]);

  useEffect(() => {
    applySnapshotData();
  }, [points, ctx.epochStart, ctx.epochEnd]);

  useEffect(() => {
    applySnapshotAnnotations();
  }, [ctx.beatPrice, ctx.orderTs, points]);

  return (
    <div className="trade-chart-strip">
      <div className="trade-chart-strip-head">
        <span className="trade-chart-ex">{feedDisplayLabel(feedId)}</span>
        <span className="trade-chart-asset">{asset}</span>
        <span className="trade-chart-legend">
          {ctx.beatPrice != null && (
            <span className="trade-chart-legend-beat" title="Feed price at slug open">
              {feedDisplayLabel(feedId)} beat ${formatPrice(ctx.beatPrice, asset)}
            </span>
          )}
          {ctx.orderTs != null && (
            <span className="trade-chart-legend-buy" title="Order fill time">
              Buy {formatEtTime(Math.floor(ctx.orderTs) as UTCTimestamp)} ET
            </span>
          )}
        </span>
      </div>
      {renderError ? (
        <p className="trade-chart-msg">{renderError}</p>
      ) : (
        <div
          className="trade-chart-panels"
          title={SNAPSHOT_CHART_INTERACTION_HINT}
          onWheel={(e) => e.stopPropagation()}
        >
          <div ref={priceRef} className="trade-chart-panel trade-chart-price" />
          <div ref={momRef} className="trade-chart-panel trade-chart-mom" />
        </div>
      )}
    </div>
  );
}

export function TradeChartModal({
  trade,
  onClose,
  variant = 'bot',
  title,
}: Props) {
  const [payload, setPayload] = useState<TradeChartPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const payloadRef = useRef<TradeChartPayload | null>(null);
  payloadRef.current = payload;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    setPayload(null);
    if (!trade.chart_id) {
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;

    const load = async () => {
      try {
        const data = await fetchTradeChart(trade.chart_id!, variant);
        if (!cancelled) setPayload(data);
      } catch {
        if (!cancelled) setPayload({ error: 'failed to load' });
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    const pollId = window.setInterval(() => {
      if (cancelled || !snapshotNeedsMoreData(payloadRef.current)) return;
      void fetchTradeChart(trade.chart_id!, variant)
        .then((data) => {
          if (!cancelled && !data.error) setPayload(data);
        })
        .catch(() => {});
    }, 2000);

    return () => {
      cancelled = true;
      window.clearInterval(pollId);
    };
  }, [trade.chart_id, trade.ts, trade.slug, variant]);

  const ctx = resolveChartContext(trade, payload);
  const series = payload?.series ?? {};
  const entries = Object.entries(series).filter(([, pts]) => pts.length > 0);
  const pmBeat =
    payload?.polymarket_beat != null && Number.isFinite(Number(payload.polymarket_beat))
      ? Number(payload.polymarket_beat)
      : null;

  const slugLabel = trade.slug;
  const intervalLabel = parseMarketSlug(trade.slug)?.interval ?? payload?.interval ?? '';

  const modalTitle = title ?? (variant === 'monitor' ? 'Profile buy snapshot' : 'Order snapshot');
  const monitorTarget =
    variant === 'monitor' ? (payload?.monitor?.target ?? (trade as TradeRow & { target?: string }).target) : null;

  const modal = (
    <div className="trade-chart-overlay" role="presentation" onClick={onClose}>
      <div
        className="trade-chart-modal"
        role="dialog"
        aria-labelledby="trade-chart-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="trade-chart-modal-head">
          <div>
            <h3 id="trade-chart-title">{modalTitle}</h3>
            <p className="trade-chart-sub">
              {formatEtDateTime(trade.ts)} ET · {trade.asset.toUpperCase()} {intervalLabel} ·{' '}
              <span className={trade.side === 'up' ? 'dir-up' : trade.side === 'down' ? 'dir-down' : ''}>
                {trade.side}
              </span>
              {variant === 'bot' && trade.result ? ` · ${trade.result}` : ''}
              {variant === 'bot' && trade.price_delta_usd != null
                ? ` · Δ=${trade.price_delta_usd >= 0 ? '+' : ''}$${trade.price_delta_usd.toFixed(4)}`
                : ''}
              {variant === 'bot' && trade.signal_feed ? ` · ${trade.signal_feed}` : ''}
              {monitorTarget ? ` · @${monitorTarget}` : ''}
              {ctx.beatPrice != null ? ` · Beat $${formatPrice(ctx.beatPrice, trade.asset)}` : ''}
              {pmBeat != null ? ` · PM $${formatPrice(pmBeat, trade.asset)}` : ''}
            </p>
            <p className="trade-chart-sub trade-chart-slug-line">
              {slugLabel}
              {ctx.epochStart > 0
                ? ` · window ${formatEtTime(ctx.epochStart as UTCTimestamp)}–${formatEtTime(ctx.epochEnd as UTCTimestamp)} ET`
                : ''}
            </p>
          </div>
          <button type="button" className="trade-chart-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <div className="trade-chart-body">
          {loading && <p className="trade-chart-msg">Loading chart…</p>}
          {!loading && !trade.chart_id && (
            <p className="trade-chart-msg">No chart captured for this trade (recorded before snapshot feature).</p>
          )}
          {!loading && trade.chart_id && payload?.error && (
            <p className="trade-chart-msg">
              {payload.error === 'not found' || payload.error === 'Not Found'
                ? variant === 'monitor'
                  ? 'Chart not found — run make server && make monitor when the buy occurred.'
                  : 'Chart not found — restart make server && make bot, then take a new trade.'
                : `Chart unavailable: ${payload.error}`}
            </p>
          )}
          {!loading && entries.length === 0 && trade.chart_id && !payload?.error && (
            <p className="trade-chart-msg">
              Empty chart data — dashboard server must run during the market epoch to capture feeds.
            </p>
          )}
          {entries.map(([key, pts]) => {
            const [feedId, asset] = key.split(':');
            const feedCtx = resolveChartContext(trade, payload, feedId);
            return (
              <SnapshotCharts
                key={key}
                feedId={feedId}
                asset={asset ?? trade.asset.toUpperCase()}
                points={pts}
                ctx={feedCtx}
              />
            );
          })}
        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
