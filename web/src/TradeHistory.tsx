import { useEffect, useState } from 'react';
import { formatEtDateTime } from './timeEt';
import { parseMarketSlug, polymarketEventUrl } from './slugNav';
import { TradeChartModal } from './TradeChartModal';

export type TradeRow = {
  ts: string;
  mode: string;
  asset: string;
  interval: string;
  slug: string;
  side: string;
  price: number;
  shares: number;
  size_usd?: number;
  position_size_mode?: string;
  best_ask: number;
  feed_price?: number;
  price_delta_usd?: number;
  signal_feed?: string;
  order_style: string;
  order_type: string;
  status: string;
  decision?: string;
  detail?: string;
  result?: 'win' | 'loss' | 'pending';
  resolved_side?: string | null;
  chart_id?: string;
  row_id?: string;
  fill_count?: number;
};

type TradeStats = {
  total: number;
  wins: number;
  losses: number;
  pending: number;
  resolved: number;
  win_rate: number | null;
  win_rate_pct: number | null;
  total_pnl?: number | null;
};

type BotStatus = {
  state?: string;
  mode?: string;
  slug?: string;
  slugs?: Record<string, { slug: string; epoch_end: string }>;
  updated_at?: string;
};

type Props = {
  focusSlug?: string | null;
  onSlugClick?: (slug: string) => void;
};

export function TradeHistory({ focusSlug = null, onSlugClick }: Props) {
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [stats, setStats] = useState<TradeStats | null>(null);
  const [status, setStatus] = useState<BotStatus>({});
  const [selectedTrade, setSelectedTrade] = useState<TradeRow | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [tRes, sRes] = await Promise.all([
          fetch('/api/bot/trades'),
          fetch('/api/bot/status'),
        ]);
        if (tRes.ok) {
          const d = await tRes.json();
          setTrades(d.trades ?? []);
          setStats(d.stats ?? null);
        }
        if (sRes.ok) setStatus(await sRes.json());
      } catch {
        /* ignore */
      }
    };
    load();
    const id = window.setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  const activeSlugs = Object.entries(status.slugs ?? {});

  function tradeRowId(t: TradeRow): string {
    return t.row_id ?? `${t.slug}:${t.side}`;
  }

  return (
    <section className="trade-history">
      <div className="trade-head">
        <h2>Trading history</h2>
        <div className="trade-head-right">
          {stats && stats.total > 0 && (
            <div className="trade-summary-stats">
              <span className="trade-win-rate" title="Wins / resolved markets">
                Win rate{' '}
                <strong>{stats.win_rate_pct != null ? `${stats.win_rate_pct}%` : '—'}</strong>
                <span className="trade-win-rate-detail">
                  ({stats.wins}W / {stats.losses}L
                  {stats.pending > 0 ? ` · ${stats.pending} pending` : ''})
                </span>
              </span>
              <span className="trade-total-pnl" title="Total PnL on resolved trades">
                PnL <strong className={pnlClass(stats.total_pnl)}>{formatPnl(stats.total_pnl)}</strong>
              </span>
            </div>
          )}
          <span className="trade-meta">
            Bot: {status.state ?? 'idle'}
            {status.mode ? ` · ${status.mode}` : ''}
            {status.updated_at ? ` · ${formatEtDateTime(status.updated_at)} ET` : ''}
          </span>
        </div>
      </div>
      {activeSlugs.length > 0 && (
        <div className="trade-active-slugs">
          {activeSlugs.map(([key, c]) => (
            <button
              key={key}
              type="button"
              className={`trade-slug-pill ${focusSlug === c.slug ? 'trade-slug-pill-active' : ''}`}
              title={`${c.epoch_end} · view charts`}
              onClick={() => onSlugClick?.(c.slug)}
            >
              <strong>{key}</strong>
              <code>{c.slug}</code>
            </button>
          ))}
        </div>
      )}
      <div className="trade-table-wrap">
        <table className="trade-table">
          <thead>
            <tr>
              <th>Time (ET)</th>
              <th>Mode</th>
              <th>Slug</th>
              <th>Asset</th>
              <th>Int</th>
              <th>Side</th>
              <th>Price</th>
              <th>Size</th>
              <th>Δ USD</th>
              <th>Feed</th>
              <th>Decision</th>
              <th>Result</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={13} className="trade-empty">
                  No trades yet — run <code>make bot</code>
                </td>
              </tr>
            ) : (
              trades.map((t) => (
                <tr
                  key={tradeRowId(t)}
                  className={`trade-row ${selectedTrade && tradeRowId(selectedTrade) === tradeRowId(t) ? 'trade-row-selected' : ''}`}
                  onClick={() => setSelectedTrade(t)}
                  title={
                    t.chart_id
                      ? t.fill_count && t.fill_count > 1
                        ? `${t.fill_count} fills merged · click for chart`
                        : 'Click to view price/momentum at order time'
                      : 'No chart snapshot'
                  }
                >
                  <td>{formatEtDateTime(t.ts)}</td>
                  <td>{t.mode}</td>
                  <td className="trade-slug-cell">
                    <SlugLink slug={t.slug} active={focusSlug === t.slug} />
                  </td>
                  <td>{t.asset.toUpperCase()}</td>
                  <td>{t.interval}</td>
                  <td className={t.side === 'up' ? 'dir-up' : 'dir-down'}>{t.side}</td>
                  <td>{t.price}</td>
                  <td>
                    {t.position_size_mode === 'usd'
                      ? `$${(t.size_usd ?? t.shares * t.price).toFixed(2)}`
                      : t.shares}
                    {t.fill_count && t.fill_count > 1 ? (
                      <span className="monitor-fill-count" title={`${t.fill_count} fills merged`}>
                        ×{t.fill_count}
                      </span>
                    ) : null}
                  </td>
                  <td className={deltaUsdClass(t.price_delta_usd)}>
                    {formatDeltaUsd(t.price_delta_usd)}
                  </td>
                  <td>{t.signal_feed ?? '—'}</td>
                  <td className="trade-decision" title={t.detail}>
                    {t.decision ?? '—'}
                  </td>
                  <td>
                    <ResultBadge result={t.result} resolvedSide={t.resolved_side} />
                  </td>
                  <td>{t.status}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {selectedTrade && (
        <TradeChartModal trade={selectedTrade} onClose={() => setSelectedTrade(null)} />
      )}
    </section>
  );
}

function formatPnl(pnl: number | null | undefined): string {
  if (pnl == null || Number.isNaN(pnl)) return '—';
  const sign = pnl > 0 ? '+' : '';
  return `${sign}$${pnl.toFixed(2)}`;
}

function pnlClass(pnl: number | null | undefined): string {
  if (pnl == null || Number.isNaN(pnl) || pnl === 0) return '';
  return pnl > 0 ? 'pnl-pos' : 'pnl-neg';
}

function deltaUsdClass(delta: number | null | undefined): string {
  if (delta == null || Number.isNaN(delta)) return '';
  return delta >= 0 ? 'roc-pos' : 'roc-neg';
}

function formatDeltaUsd(delta: number | null | undefined): string {
  if (delta == null || Number.isNaN(delta)) return '—';
  const sign = delta > 0 ? '+' : '';
  return `${sign}$${delta.toFixed(4)}`;
}

function ResultBadge({
  result,
  resolvedSide,
}: {
  result?: TradeRow['result'];
  resolvedSide?: string | null;
}) {
  if (!result || result === 'pending') {
    return <span className="result-pending">pending</span>;
  }
  const title =
    resolvedSide != null ? `Market resolved ${resolvedSide.toUpperCase()}` : undefined;
  return (
    <span className={result === 'win' ? 'result-win' : 'result-loss'} title={title}>
      {result}
    </span>
  );
}

function SlugLink({ slug, active }: { slug: string; active?: boolean }) {
  const parsed = parseMarketSlug(slug);
  const title = parsed
    ? `Open ${parsed.asset.toUpperCase()} ${parsed.interval} on Polymarket`
    : 'Open on Polymarket';

  return (
    <a
      href={polymarketEventUrl(slug)}
      target="_blank"
      rel="noopener noreferrer"
      className={`trade-slug-link ${active ? 'trade-slug-link-active' : ''}`}
      title={title}
      onClick={(e) => e.stopPropagation()}
    >
      <code>{slug}</code>
    </a>
  );
}
