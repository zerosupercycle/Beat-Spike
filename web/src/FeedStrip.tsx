import { useEffect, useRef } from 'react';
import {
  ColorType,
  createChart,
  LineSeries,
  LineStyle,
  LineType,
  type AutoscaleInfo,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type UTCTimestamp,
} from 'lightweight-charts';
import { FEED_COLORS, formatMomentumDisplay, type HistoryStore } from './history';
import { feedDisplayLabel } from './feeds';
import {
  CHART_INTERACTION_HINT,
  clampVisibleTimeRange,
  MIN_VISIBLE_RANGE_SEC,
} from './chartTimeRange';
import { formatEtTick, formatEtTime } from './timeEt';
import type { AssetRow, Direction } from './types';

function stableBeatPrice(beat: number | null | undefined, asset: string): number | null {
  if (beat == null || !Number.isFinite(beat)) return null;
  const factor = asset === 'XRP' ? 10000 : 100;
  return Math.round(beat * factor) / factor;
}

function createBeatAutoscaleProvider(beatPriceRef: React.RefObject<number | null>) {
  return (original: () => AutoscaleInfo | null) => {
    const res = original();
    const beatPrice = beatPriceRef.current;
    if (res === null || res.priceRange === null || beatPrice == null || !Number.isFinite(beatPrice)) {
      return res;
    }
    const { minValue, maxValue } = res.priceRange;
    // Beat already in view — leave autoscale to price data only (avoids jitter).
    if (beatPrice >= minValue && beatPrice <= maxValue) {
      return res;
    }
    const pad = Math.max((maxValue - minValue) * 0.04, Math.abs(beatPrice) * 0.0001, 0.01);
    if (beatPrice < minValue) {
      return {
        priceRange: {
          minValue: beatPrice - pad,
          maxValue: maxValue + pad * 0.1,
        },
      };
    }
    return {
      priceRange: {
        minValue: minValue - pad * 0.1,
        maxValue: beatPrice + pad,
      },
    };
  };
}

type Props = {
  asset: string;
  feedId: string;
  feedLabel: string;
  row: AssetRow | undefined;
  healthState: string;
  healthStale: boolean;
  storeRef: React.MutableRefObject<HistoryStore>;
  tick: number;
  highlight?: boolean;
  beatPrice?: number | null;
  beatSlug?: string | null;
  beatInterval?: string;
  beatDeltaUsd?: number | null;
};

function formatPrice(n: number | null | undefined, asset: string): string {
  if (n == null || !Number.isFinite(n)) return '—';
  const digits = asset === 'XRP' ? 4 : 2;
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function dirClass(d: Direction | string | undefined): string {
  if (d === 'up') return 'dir-up';
  if (d === 'down') return 'dir-down';
  return 'dir-neutral';
}

function healthDot(state: string, stale: boolean): string {
  if (state === 'connected' && !stale) return 'dot-ok';
  if (state === 'connecting' || state === 'reconnecting') return 'dot-warn';
  return 'dot-bad';
}

function toLineData(
  store: HistoryStore,
  seriesKey: string,
  pick: 'price' | 'momentum',
): LineData<UTCTimestamp>[] {
  const pts = store.get(seriesKey) ?? [];
  return pts.map((p) => ({
    time: p.time,
    value: pick === 'price' ? p.price : p.momentum,
  }));
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

const SYNC_MS = 50;

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
  const prec = asset === 'XRP' ? 4 : 2;
  return {
    type: 'price' as const,
    precision: prec,
    minMove: asset === 'XRP' ? 0.0001 : 0.01,
  };
}

function useSyncedPriceMomCharts(
  priceRef: React.RefObject<HTMLDivElement | null>,
  momRef: React.RefObject<HTMLDivElement | null>,
  seriesKey: string,
  storeRef: React.MutableRefObject<HistoryStore>,
  tick: number,
  color: string,
  asset: string,
  beatPrice: number | null | undefined,
  beatSlug: string | null | undefined,
) {
  const priceChartRef = useRef<IChartApi | null>(null);
  const momChartRef = useRef<IChartApi | null>(null);
  const priceSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const momSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const beatLineRef = useRef<IPriceLine | null>(null);
  const beatPriceRef = useRef<number | null>(null);
  const lastStableBeatRef = useRef<number | null>(null);
  const lastBeatSlugRef = useRef<string | null>(null);
  const beatAutoscaleRef = useRef(createBeatAutoscaleProvider(beatPriceRef));
  beatPriceRef.current = stableBeatPrice(beatPrice, asset);
  const lastSigRef = useRef({ price: '', mom: '' });
  const followLiveRef = useRef(true);
  const programmaticRangeRef = useRef(false);
  const rangeSyncSourceRef = useRef<'none' | 'mom' | 'price'>('none');

  const syncTimeFromMom = () => {
    const mom = momChartRef.current;
    const price = priceChartRef.current;
    if (!mom || !price) return;
    rangeSyncSourceRef.current = 'mom';
    const range = mom.timeScale().getVisibleLogicalRange();
    if (range) price.timeScale().setVisibleLogicalRange(range);
    rangeSyncSourceRef.current = 'none';
  };

  const syncTimeFromPrice = () => {
    const mom = momChartRef.current;
    const price = priceChartRef.current;
    if (!mom || !price) return;
    rangeSyncSourceRef.current = 'price';
    const range = price.timeScale().getVisibleLogicalRange();
    if (range) mom.timeScale().setVisibleLogicalRange(range);
    rangeSyncSourceRef.current = 'none';
  };

  const onMomVisibleRangeChange = () => {
    const mom = momChartRef.current;
    if (!mom || rangeSyncSourceRef.current === 'price') return;
    if (!programmaticRangeRef.current) {
      followLiveRef.current = false;
      clampVisibleTimeRange(mom, MIN_VISIBLE_RANGE_SEC);
    }
    syncTimeFromMom();
  };

  const onPriceVisibleRangeChange = () => {
    const mom = momChartRef.current;
    const price = priceChartRef.current;
    if (!mom || !price || rangeSyncSourceRef.current === 'mom') return;
    if (!programmaticRangeRef.current) {
      followLiveRef.current = false;
      syncTimeFromPrice();
      clampVisibleTimeRange(mom, MIN_VISIBLE_RANGE_SEC);
      syncTimeFromMom();
    }
  };

  const applyData = () => {
    const priceSeries = priceSeriesRef.current;
    const momSeries = momSeriesRef.current;
    const momChart = momChartRef.current;
    if (!priceSeries || !momSeries || !momChart) return;

    const priceRaw = toLineData(storeRef.current, seriesKey, 'price');
    const momRaw = toLineData(storeRef.current, seriesKey, 'momentum');
    if (priceRaw.length === 0 && momRaw.length === 0) return;

    try {
      if (priceRaw.length > 0) {
        const last = priceRaw[priceRaw.length - 1];
        const sig = `${priceRaw.length}|${last.time}|${last.value}`;
        if (sig !== lastSigRef.current.price) {
          lastSigRef.current.price = sig;
          priceSeries.setData(withMinPoints(priceRaw));
        }
      }
      if (momRaw.length > 0) {
        const last = momRaw[momRaw.length - 1];
        const sig = `${momRaw.length}|${last.time}|${last.value}`;
        if (sig !== lastSigRef.current.mom) {
          lastSigRef.current.mom = sig;
          momSeries.setData(withMinPoints(momRaw));
        }
      }
      programmaticRangeRef.current = true;
      if (followLiveRef.current) {
        momChart.timeScale().scrollToRealTime();
      }
      syncTimeFromMom();
      programmaticRangeRef.current = false;
    } catch {
      lastSigRef.current = { price: '', mom: '' };
    }
  };

  useEffect(() => {
    const priceEl = priceRef.current;
    const momEl = momRef.current;
    if (!priceEl || !momEl) return;

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
        crosshair: { mode: 0, vertLine: { visible: false }, horzLine: { visible: false } },
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
        rightPriceScale: {
          visible: true,
          borderVisible: false,
          minimumWidth: 64,
          scaleMargins: { top: 0.1, bottom: 0.08 },
        },
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
        rightPriceScale: {
          visible: true,
          borderVisible: false,
          minimumWidth: 64,
          scaleMargins: { top: 0.1, bottom: 0.12 },
        },
      });

      priceSeriesRef.current = priceChart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        lineType: LineType.Curved,
        priceLineVisible: true,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
        priceFormat: pricePriceFormat(asset),
        autoscaleInfoProvider: beatAutoscaleRef.current,
      });
      momSeriesRef.current = momChart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        lineType: LineType.Curved,
        priceLineVisible: true,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
        priceFormat: momPriceFormat(),
      });

      priceChartRef.current = priceChart;
      momChartRef.current = momChart;
      lastSigRef.current = { price: '', mom: '' };

      momChart.timeScale().subscribeVisibleLogicalRangeChange(onMomVisibleRangeChange);
      priceChart.timeScale().subscribeVisibleLogicalRangeChange(onPriceVisibleRangeChange);
      const stableBeat = beatPriceRef.current;
      if (stableBeat != null) {
        beatLineRef.current = priceSeriesRef.current.createPriceLine({
          price: stableBeat,
          color: '#f59e0b',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: 'Beat',
        });
        lastStableBeatRef.current = stableBeat;
      }
      applyData();
    };

    const ro = new ResizeObserver(() => {
      if (disposed) return;
      if (!priceChartRef.current) {
        mount();
        return;
      }
      const pw = priceEl.clientWidth;
      const ph = priceEl.clientHeight;
      const mw = momEl.clientWidth;
      const mh = momEl.clientHeight;
      if (pw > 0 && ph > 0) priceChartRef.current.applyOptions({ width: pw, height: ph });
      if (mw > 0 && mh > 0) momChartRef.current?.applyOptions({ width: mw, height: mh });
      syncTimeFromMom();
    });

    ro.observe(priceEl);
    ro.observe(momEl);
    requestAnimationFrame(mount);

    const interval = window.setInterval(applyData, SYNC_MS);

    return () => {
      disposed = true;
      clearInterval(interval);
      ro.disconnect();
      priceChartRef.current?.remove();
      momChartRef.current?.remove();
      priceChartRef.current = null;
      momChartRef.current = null;
      priceSeriesRef.current = null;
      momSeriesRef.current = null;
      beatLineRef.current = null;
      lastStableBeatRef.current = null;
      lastBeatSlugRef.current = null;
      lastSigRef.current = { price: '', mom: '' };
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [priceRef, momRef, color, seriesKey, asset]);

  useEffect(() => {
    applyData();
  }, [tick, seriesKey, storeRef]);

  useEffect(() => {
    const series = priceSeriesRef.current;
    const stable = stableBeatPrice(beatPrice, asset);
    beatPriceRef.current = stable;
    if (!series) return;

    const slugKey = beatSlug?.trim().toLowerCase() ?? null;
    const slugChanged = slugKey !== lastBeatSlugRef.current;
    if (slugChanged) {
      lastBeatSlugRef.current = slugKey;
      lastStableBeatRef.current = null;
    }
    if (stable === lastStableBeatRef.current && !slugChanged) return;
    lastStableBeatRef.current = stable;

    if (stable == null) {
      if (beatLineRef.current) {
        series.removePriceLine(beatLineRef.current);
        beatLineRef.current = null;
      }
      return;
    }

    if (beatLineRef.current) {
      beatLineRef.current.applyOptions({ price: stable });
      return;
    }

    const feedId = seriesKey.split(':')[0] ?? '';
    const beatLabel = `${feedDisplayLabel(feedId)} beat`;
    beatLineRef.current = series.createPriceLine({
      price: stable,
      color,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: beatLabel,
    });
  }, [beatPrice, beatSlug, seriesKey, asset, color]);
}

function ChartOverlay({
  text,
  direction,
  className,
}: {
  text: string;
  direction?: Direction;
  className?: string;
}) {
  if (text === '—') return null;
  return (
    <span className={`chart-coord-label ${dirClass(direction)} ${className ?? ''}`} title="Latest value">
      {text}
    </span>
  );
}

function formatBeatDelta(deltaPct: number | null | undefined): string {
  if (deltaPct == null || Number.isNaN(deltaPct)) return '';
  const sign = deltaPct > 0 ? '+' : '';
  return `${sign}${deltaPct.toFixed(4)}%`;
}

export function FeedStrip({
  asset,
  feedId,
  feedLabel,
  row,
  healthState,
  healthStale,
  storeRef,
  tick,
  highlight = false,
  beatPrice = null,
  beatSlug = null,
  beatInterval = '5m',
  beatDeltaUsd = null,
}: Props) {
  const priceRef = useRef<HTMLDivElement>(null);
  const momRef = useRef<HTMLDivElement>(null);
  const seriesKey = `${feedId}:${asset}`;
  const color = FEED_COLORS[feedId] ?? '#94a3b8';
  const exShort = feedDisplayLabel(feedId, feedLabel);
  const stableBeat = stableBeatPrice(beatPrice, asset);

  useSyncedPriceMomCharts(priceRef, momRef, seriesKey, storeRef, tick, color, asset, stableBeat, beatSlug);

  const price = row?.price;
  const mom = row?.momentum;
  const dir = mom?.aggregate_direction;
  const momDisplay = formatMomentumDisplay(mom);

  return (
    <article className={`cex-strip ${highlight ? 'cex-strip-signal' : ''}`}>
      <div className="cex-strip-head">
        <span className={`dot ${healthDot(healthState, healthStale)}`} />
        <span className="cex-strip-title">
          {exShort}
          {highlight ? <span className="cex-strip-signal-tag">signal</span> : null}
        </span>
        <span className={`cex-strip-dir ${dirClass(dir)}`}>{dir ?? '—'}</span>
      </div>
      <div className="cex-strip-values">
        <span className="cex-strip-price">${formatPrice(price, asset)}</span>
        {stableBeat != null && (
          <span
            className="cex-strip-beat"
            title={`Beat at slug open (${beatInterval}) — this feed's price when the market started`}
          >
            Beat ${formatPrice(stableBeat, asset)}
            {beatDeltaUsd != null && Number.isFinite(beatDeltaUsd)
              ? ` · Δ ${beatDeltaUsd >= 0 ? '+' : ''}$${Math.abs(beatDeltaUsd).toFixed(2)}`
              : ''}
          </span>
        )}
        <span
          className={`cex-strip-mom ${dirClass(momDisplay.direction)}`}
          title={`${momDisplay.label} (4s window)`}
        >
          <span className="cex-strip-mom-label">{momDisplay.label}</span>
          {momDisplay.text}
        </span>
      </div>
      <div className="cex-strip-charts">
        <div className="cex-chart-pane cex-chart-price" title={CHART_INTERACTION_HINT}>
          {stableBeat != null && (
            <ChartOverlay
              text={`Beat $${formatPrice(stableBeat, asset)}`}
              className="chart-coord-beat"
            />
          )}
          <ChartOverlay
            text={price != null ? `$${formatPrice(price, asset)}` : '—'}
            className="chart-coord-price"
          />
          <div className="chart-surface" ref={priceRef} />
        </div>
        <div
          className="cex-chart-pane cex-chart-mom"
          title={`Momentum ROC % · ${CHART_INTERACTION_HINT}`}
        >
          <ChartOverlay
            text={momDisplay.text}
            direction={momDisplay.direction}
            className="chart-coord-mom"
          />
          <div className="chart-surface" ref={momRef} />
        </div>
      </div>
    </article>
  );
}
