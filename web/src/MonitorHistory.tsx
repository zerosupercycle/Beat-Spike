import { useEffect, useState } from 'react';
import { formatEtDateTime } from './timeEt';
import { parseMarketSlug, polymarketEventUrl } from './slugNav';
import { TradeChartModal } from './TradeChartModal';
import type { TradeRow } from './TradeHistory';

type MonitorStats = {
  total: number;
  wins: number;
  losses: number;
  pending: number;
  resolved: number;
  win_rate: number | null;
  win_rate_pct: number | null;
  total_pnl?: number | null;
};

export type MonitorEventRow = {
  ts: string;
  target: string;
  target_url: string;
  proxy_wallet: string;
  slug: string;
  asset: string;
  interval: string;
  side: string;
  price: number;
  size: number;
  usdc_size?: number;
  outcome: string;
  transaction_hash: string;
  chart_id?: string;
  row_id?: string;
  fill_count?: number;
  result?: 'win' | 'loss' | 'pending';
  resolved_side?: string | null;
};

type MonitorTarget = {
  handle: string;
  url: string;
  stats?: MonitorStats;
};

function toChartTrade(event: MonitorEventRow): TradeRow & { target?: string } {
  return {
    ts: event.ts,
    mode: 'monitor',
    asset: event.asset,
    interval: event.interval,
    slug: event.slug,
    side: (event.outcome || event.side || 'buy').toLowerCase(),
    price: event.price,
    shares: event.size,
    size_usd: event.usdc_size ?? event.price * event.size,
    position_size_mode: 'usd',
    best_ask: event.price,
    order_style: '—',
    order_type: '—',
    status: 'observed',
    chart_id: event.chart_id,
    target: event.target,
  };
}

function monitorRowId(event: MonitorEventRow): string {
  return event.row_id ?? `${event.target}:${event.slug}`;
}

export function MonitorHistory() {
  const [events, setEvents] = useState<MonitorEventRow[]>([]);
  const [targets, setTargets] = useState<MonitorTarget[]>([]);
  const [selected, setSelected] = useState<MonitorEventRow | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch('/api/monitor/events?limit=100');
        if (!res.ok) return;
        const data = await res.json();
        setEvents(data.events ?? []);
        setTargets(data.targets ?? []);
      } catch {
        /* ignore */
      }
    };
    load();
    const id = window.setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  return (
    <section className="trade-history monitor-history">
      <div className="trade-head">
        <h2>Profile monitor</h2>
        <div className="trade-head-right">
          {targets.length > 0 ? (
            <div className="monitor-profile-stats">
              {targets.map((t) => (
                <ProfileStatsRow key={t.handle} target={t} />
              ))}
            </div>
          ) : (
            <span className="trade-meta">No profiles configured</span>
          )}
        </div>
      </div>
      <div className="trade-table-wrap">
        <table className="trade-table">
          <thead>
            <tr>
              <th>Time (ET)</th>
              <th>Target</th>
              <th>Slug</th>
              <th>Asset</th>
              <th>Int</th>
              <th>Outcome</th>
              <th>Price</th>
              <th>Size</th>
              <th>USDC</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {events.length === 0 ? (
              <tr>
                <td colSpan={10} className="trade-empty">
                  No monitored buys yet — run <code>make server</code> then <code>make monitor</code>
                </td>
              </tr>
            ) : (
              events.map((e) => (
                <tr
                  key={monitorRowId(e)}
                  className={`trade-row ${selected && monitorRowId(selected) === monitorRowId(e) ? 'trade-row-selected' : ''}`}
                  onClick={() => setSelected(e)}
                  title={
                    e.chart_id
                      ? e.fill_count && e.fill_count > 1
                        ? `${e.fill_count} fills merged · click for price snapshot`
                        : 'Click to view ±2m price snapshot'
                      : 'No chart snapshot'
                  }
                >
                  <td>{formatEtDateTime(e.ts)}</td>
                  <td>
                    <a
                      href={e.target_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="monitor-target-link"
                      onClick={(ev) => ev.stopPropagation()}
                    >
                      @{e.target}
                    </a>
                  </td>
                  <td className="trade-slug-cell">
                    <SlugLink slug={e.slug} />
                  </td>
                  <td>{e.asset.toUpperCase()}</td>
                  <td>{e.interval}</td>
                  <td className={outcomeDirClass(e.outcome)}>{e.outcome || e.side}</td>
                  <td>{e.price}</td>
                  <td>
                    {e.size}
                    {e.fill_count && e.fill_count > 1 ? (
                      <span className="monitor-fill-count" title={`${e.fill_count} fills merged`}>
                        ×{e.fill_count}
                      </span>
                    ) : null}
                  </td>
                  <td>${(e.usdc_size ?? e.price * e.size).toFixed(2)}</td>
                  <td>
                    <ResultBadge result={e.result} resolvedSide={e.resolved_side} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {selected && (
        <TradeChartModal
          trade={toChartTrade(selected)}
          variant="monitor"
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  );
}

function ProfileStatsRow({ target }: { target: MonitorTarget }) {
  const stats = target.stats;
  const hasStats = stats != null && stats.total > 0;
  return (
    <div className="monitor-profile-row">
      <a
        href={target.url}
        target="_blank"
        rel="noopener noreferrer"
        className="monitor-target-link monitor-profile-handle"
      >
        @{target.handle}
      </a>
      {hasStats ? (
        <ProfileStatsSummary stats={stats} compact />
      ) : (
        <span className="trade-meta">watching · no buys yet</span>
      )}
    </div>
  );
}

function ProfileStatsSummary({ stats, compact = false }: { stats: MonitorStats; compact?: boolean }) {
  return (
    <div className={`trade-summary-stats${compact ? ' trade-summary-stats-compact' : ''}`}>
      <span className="trade-win-rate" title="Wins / resolved markets">
        Win rate{' '}
        <strong>{stats.win_rate_pct != null ? `${stats.win_rate_pct}%` : '—'}</strong>
        <span className="trade-win-rate-detail">
          ({stats.wins}W / {stats.losses}L
          {stats.pending > 0 ? ` · ${stats.pending} pending` : ''})
        </span>
      </span>
      <span className="trade-total-pnl" title="Total PnL on resolved buys">
        PnL <strong className={pnlClass(stats.total_pnl)}>{formatPnl(stats.total_pnl)}</strong>
      </span>
    </div>
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

function outcomeDirClass(outcome: string): string {
  const o = outcome.toLowerCase();
  if (o === 'up') return 'dir-up';
  if (o === 'down') return 'dir-down';
  return '';
}

function ResultBadge({
  result,
  resolvedSide,
}: {
  result?: MonitorEventRow['result'];
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

function SlugLink({ slug }: { slug: string }) {
  const parsed = parseMarketSlug(slug);
  const title = parsed
    ? `${parsed.asset.toUpperCase()} ${parsed.interval} on Polymarket`
    : 'Open on Polymarket';

  return (
    <a
      href={polymarketEventUrl(slug)}
      target="_blank"
      rel="noopener noreferrer"
      className="trade-slug-link"
      title={title}
      onClick={(e) => e.stopPropagation()}
    >
      <code>{slug}</code>
    </a>
  );
}
