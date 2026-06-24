/** Polymarket crypto up/down slugs: btc-updown-5m-1780601700 */

const SLUG_RE = /^([a-z]+)-updown-(5m|15m|1h)-(\d+)$/;

export type ParsedSlug = {
  asset: string;
  interval: string;
  epochTs: number;
};

export function parseMarketSlug(slug: string): ParsedSlug | null {
  const m = slug.trim().toLowerCase().match(SLUG_RE);
  if (!m) return null;
  return { asset: m[1], interval: m[2], epochTs: Number(m[3]) };
}

export function polymarketEventUrl(slug: string): string {
  return `https://polymarket.com/event/${slug.trim().toLowerCase()}`;
}

export function assetTabFromSlug(slug: string): string | null {
  const p = parseMarketSlug(slug);
  return p ? p.asset.toUpperCase() : null;
}

export function slugFromHash(hash: string): string | null {
  const s = hash.replace(/^#/, '').trim().toLowerCase();
  return s && parseMarketSlug(s) ? s : null;
}

export const INTERVAL_SECONDS: Record<string, number> = {
  '5m': 300,
  '15m': 900,
  '1h': 3600,
};

export function slugEpochBounds(slug: string): { start: number; end: number } | null {
  const p = parseMarketSlug(slug);
  if (!p) return null;
  const sec = INTERVAL_SECONDS[p.interval] ?? 300;
  return { start: p.epochTs, end: p.epochTs + sec };
}
