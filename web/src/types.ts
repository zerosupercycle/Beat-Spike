export type Direction = 'up' | 'down' | 'neutral';

export type MomentumMetric = {
  value?: number;
  direction: Direction;
  net?: number;
  ema_level?: number;
  price_minus_ema?: number;
};

export type Momentum = {
  aggregate_direction: Direction;
  absolute?: MomentumMetric;
  roc_percent?: MomentumMetric;
  regression_slope_per_s?: MomentumMetric;
  ema?: MomentumMetric & { ema_level?: number; price_minus_ema?: number };
  rsi?: MomentumMetric;
  tick_net?: MomentumMetric & { net?: number };
  samples?: number;
  window_seconds?: number;
};

export type AssetRow = {
  price: number | null;
  received_at: string | null;
  momentum: Momentum | null;
  beat?: number | null;
  delta?: number | null;
  outcome?: string | null;
};

export type FeedHealth = {
  state: string;
  endpoint: string;
  summary: string;
  data_stale: boolean;
  last_error?: string | null;
  connected_since?: string | null;
  transport?: string;
};

export type FeedSnapshot = {
  id: string;
  label: string;
  health: FeedHealth;
  assets: Record<string, AssetRow>;
};

export type AssetDetectionState = {
  price?: number | null;
  ref_price?: number | null;
  delta_usd?: number | null;
  max_abs_delta_usd?: number | null;
  side?: 'up' | 'down' | null;
  threshold_usd?: number;
  threshold_up_usd?: number;
  threshold_down_usd?: number;
  lookback_seconds?: number;
  sustain_required_sec?: number;
  sustain_elapsed_sec?: number;
  above_threshold?: boolean;
  sustain_ready?: boolean;
  feed_id?: string;
};

export type DashboardConfig = {
  assets: string[];
  intervals: string[];
  price_feed?: string;
  feeds?: string[];
  strategy?: {
    lookback_seconds?: number;
    sustain_seconds?: number;
    threshold_mode?: string;
    by_asset?: Record<
      string,
      {
        delta_threshold_usd?: number;
        lookback_seconds?: number;
        sustain_seconds?: number;
      }
    >;
  };
};

export type AssetBeat = {
  beat: number | null;
  slug?: string;
  delta_pct?: number | null;
  feed_beats?: Record<string, number | null>;
  polymarket_beat?: number | null;
};

export type DashboardSnapshot = {
  updated_at: string;
  version: number;
  assets: string[];
  feeds: Record<string, FeedSnapshot>;
  beats?: Record<string, Record<string, AssetBeat>>;
  beats_by_slug?: Record<string, AssetBeat & { asset?: string; interval?: string }>;
  detection?: Record<string, AssetDetectionState>;
  config?: DashboardConfig;
};
