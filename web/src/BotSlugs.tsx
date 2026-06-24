import { useEffect, useState } from 'react';

type CycleSlug = {
  slug: string;
  asset: string;
  interval: string;
  epoch_end: string;
};

type BotStatus = {
  state?: string;
  mode?: string;
  slug?: string;
  slugs?: Record<string, CycleSlug>;
  cycles_completed?: number;
  buys_triggered?: number;
  updated_at?: string;
};

type Props = {
  focusSlug?: string | null;
  onSlugClick?: (slug: string) => void;
};

export function BotSlugs({ focusSlug = null, onSlugClick }: Props) {
  const [status, setStatus] = useState<BotStatus>({});

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch('/api/bot/status');
        if (res.ok) setStatus(await res.json());
      } catch {
        /* ignore */
      }
    };
    load();
    const id = window.setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  const entries = Object.entries(status.slugs ?? {}).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0 && !status.slug) return null;

  return (
    <div className="bot-slugs">
      <span className="bot-slugs-label">Polymarket</span>
      {entries.length > 0 ? (
        entries.map(([key, c]) => (
          <button
            key={key}
            type="button"
            className={`bot-slug-chip ${focusSlug === c.slug ? 'bot-slug-chip-active' : ''}`}
            title={`Ends ${c.epoch_end} · view charts`}
            onClick={() => onSlugClick?.(c.slug)}
          >
            <span className="bot-slug-key">{key}</span>
            <code className="bot-slug-val">{c.slug}</code>
          </button>
        ))
      ) : (
        <span className="bot-slug-chip">
          <code className="bot-slug-val">{status.slug}</code>
        </span>
      )}
      {(status.cycles_completed != null || status.buys_triggered != null) && (
        <span className="bot-slugs-stats">
          cycles {status.cycles_completed ?? 0} · buys {status.buys_triggered ?? 0}
        </span>
      )}
    </div>
  );
}
