import type { UTCTimestamp } from 'lightweight-charts';

const ET = 'America/New_York';

/** Format unix seconds as ET clock for chart axis / tooltips. */
export function formatEtTime(ts: UTCTimestamp): string {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: ET,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(Number(ts) * 1000));
}

/** Shorter tick labels (no seconds) for dense cells. */
export function formatEtTick(ts: UTCTimestamp): string {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: ET,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(Number(ts) * 1000));
}

export function formatEtDateTime(iso: string | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat('en-US', {
    timeZone: ET,
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d);
}
