export const FEED_ORDER = ['chainlink', 'binance'] as const;

export type FeedId = (typeof FEED_ORDER)[number];

export const FEED_LABELS: Record<FeedId, string> = {
  chainlink: 'CL',
  binance: 'BN',
};

export const FEED_COLORS: Record<string, string> = {
  chainlink: '#22d3ee',
  binance: '#f59e0b',
};

export function feedDisplayLabel(feedId: string, label?: string): string {
  const short = FEED_LABELS[feedId as FeedId];
  if (short) return short;
  if (label) return label;
  return feedId;
}
