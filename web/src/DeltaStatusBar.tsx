import type { AssetDetectionState, DashboardSnapshot } from './types';

type Props = {
  snapshot: DashboardSnapshot | null;
  activeAsset: string;
  priceFeed: string;
};

function formatDelta(delta: number | null | undefined): string {
  if (delta == null || Number.isNaN(delta)) return '—';
  const sign = delta > 0 ? '+' : '';
  return `${sign}$${Math.abs(delta).toFixed(2)}`;
}

function formatThreshold(threshold: number | null | undefined): string {
  if (threshold == null || Number.isNaN(threshold)) return '—';
  return `$${threshold.toFixed(2)}`;
}

function formatSideThresholds(state: AssetDetectionState | undefined): string {
  const up = state?.threshold_up_usd;
  const down = state?.threshold_down_usd;
  if (up != null && down != null) {
    if (Math.abs(up - down) < 1e-9) return formatThreshold(up);
    return `↑${formatThreshold(up)} ↓${formatThreshold(down)}`;
  }
  return formatThreshold(state?.threshold_usd);
}

function formatSustain(elapsed: number, required: number): string {
  if (!Number.isFinite(required) || required <= 0) return '';
  const e = Math.max(0, elapsed);
  return `${e.toFixed(1)}s / ${required.toFixed(0)}s hold`;
}

function sideLabel(side: string | null | undefined): string {
  if (side === 'up') return 'UP';
  if (side === 'down') return 'DOWN';
  return '—';
}

function DeltaChip({
  asset,
  state,
  active,
}: {
  asset: string;
  state: AssetDetectionState | undefined;
  active: boolean;
}) {
  const absDelta =
    state?.max_abs_delta_usd ??
    (state?.delta_usd != null ? Math.abs(state.delta_usd) : null);
  const side = state?.side;
  const lookback = state?.lookback_seconds;
  const ready = state?.sustain_ready;
  const above = state?.above_threshold;
  const sustainRequired = state?.sustain_required_sec ?? 0;
  const sideClass = side === 'up' ? 'dir-up' : side === 'down' ? 'dir-down' : 'dir-neutral';
  const sustainLabel = formatSustain(state?.sustain_elapsed_sec ?? 0, sustainRequired);

  return (
    <div className={`delta-chip ${active ? 'delta-chip-active' : ''} ${ready ? 'delta-chip-ready' : ''}`}>
      <span className="delta-chip-asset">{asset}</span>
      <span className={`delta-chip-side ${sideClass}`}>{sideLabel(side)}</span>
      <span className="delta-chip-metric" title="|Δ| from lookback point to now vs threshold">
        |Δ| {formatDelta(absDelta)}
        <span className="delta-chip-thr"> / {formatSideThresholds(state)}</span>
      </span>
      <span className="delta-chip-window" title="Lookback: compare to price this many seconds ago">
        {lookback != null ? `${lookback}s window` : '—'}
      </span>
      {sustainLabel ? (
        <span
          className={`delta-chip-sustain ${ready ? 'delta-chip-sustain-ready' : above ? 'delta-chip-sustain-pending' : ''}`}
          title="Optional hold after threshold"
        >
          {sustainLabel}
        </span>
      ) : null}
    </div>
  );
}

export function DeltaStatusBar({ snapshot, activeAsset, priceFeed }: Props) {
  const assets = snapshot?.config?.assets?.length
    ? snapshot.config.assets
    : snapshot?.assets ?? ['BTC', 'ETH', 'SOL', 'XRP'];
  const detection = snapshot?.detection ?? {};
  const defaultLookback = snapshot?.config?.strategy?.lookback_seconds ?? 30;
  const sustain = snapshot?.config?.strategy?.sustain_seconds ?? 0;

  return (
    <section className="delta-status-bar" aria-label="Delta momentum by asset">
      <div className="delta-status-head">
        <span className="delta-status-title">Signal delta</span>
        <span className="delta-status-meta">
          {feedLabel(priceFeed)} · |Δ| vs price {defaultLookback}s ago (per-asset lookback)
          {sustain > 0 ? ` · sustain ${sustain}s` : ''}
        </span>
      </div>
      <div className="delta-status-grid">
        {assets.map((asset) => (
          <DeltaChip
            key={asset}
            asset={asset}
            state={detection[asset]}
            active={asset === activeAsset}
          />
        ))}
      </div>
    </section>
  );
}

function feedLabel(feedId: string): string {
  if (feedId === 'chainlink') return 'CL';
  if (feedId === 'binance') return 'BN';
  return feedId.toUpperCase();
}
