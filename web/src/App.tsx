import { useCallback, useEffect, useMemo, useState } from 'react';
import { BotSlugs } from './BotSlugs';
import { DeltaStatusBar } from './DeltaStatusBar';
import { FeedStrip } from './FeedStrip';
import { FEED_ORDER } from './feeds';
import { assetTabFromSlug, parseMarketSlug, slugFromHash } from './slugNav';
import { TradeHistory } from './TradeHistory';
import { MonitorHistory } from './MonitorHistory';
import type { AssetBeat } from './types';
import { useDashboardWs } from './useDashboardWs';
import './App.css';

type AssetTab = string;

export default function App() {
  const configAssets = useMemo(() => ['BTC', 'ETH', 'SOL', 'XRP'] as const, []);
  const [assetTab, setAssetTab] = useState<AssetTab>(() => {
    const slug = slugFromHash(window.location.hash);
    return (slug ? assetTabFromSlug(slug) : null) ?? 'BTC';
  });
  const [focusSlug, setFocusSlug] = useState<string | null>(() => slugFromHash(window.location.hash));
  const { snapshot, connected, error, storeRef, tick } = useDashboardWs();

  const assets = snapshot?.config?.assets?.length
    ? snapshot.config.assets
    : [...configAssets];
  const intervals = snapshot?.config?.intervals?.length ? snapshot.config.intervals : ['5m'];
  const displayFeeds = snapshot?.config?.feeds?.length
    ? snapshot.config.feeds
    : [...FEED_ORDER];
  const priceFeed = snapshot?.config?.price_feed ?? 'binance';
  const chartInterval = intervals[0] ?? '5m';
  const intervalBeat = snapshot?.beats?.[chartInterval]?.[assetTab];
  const focusParsed = focusSlug ? parseMarketSlug(focusSlug) : null;
  const useFocusSlug =
    focusParsed != null && focusParsed.asset === assetTab.toLowerCase();
  const activeSlug = (
    useFocusSlug ? focusSlug : intervalBeat?.slug ?? null
  )?.trim().toLowerCase() ?? null;
  const beatFromSlug =
    activeSlug && snapshot?.beats_by_slug?.[activeSlug]
      ? snapshot.beats_by_slug[activeSlug]
      : null;
  const [fetchedBeat, setFetchedBeat] = useState<AssetBeat | null>(null);

  useEffect(() => {
    if (!activeSlug) {
      setFetchedBeat(null);
      return;
    }
    if (beatFromSlug?.feed_beats && Object.keys(beatFromSlug.feed_beats).length > 0) {
      setFetchedBeat(null);
      return;
    }
    if (intervalBeat?.slug === activeSlug && intervalBeat?.feed_beats) {
      setFetchedBeat(null);
      return;
    }
    let cancelled = false;
    fetch(`/api/beat?slug=${encodeURIComponent(activeSlug)}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((row) => {
        if (!cancelled && row) setFetchedBeat(row as AssetBeat);
      })
      .catch(() => {
        if (!cancelled) setFetchedBeat(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSlug, beatFromSlug?.beat, intervalBeat?.slug]);

  const beatInfo = beatFromSlug ?? fetchedBeat ?? intervalBeat;
  const slugFeedBeats = beatInfo?.feed_beats;

  const navigateToSlug = useCallback((slug: string) => {
    const normalized = slug.trim().toLowerCase();
    const tab = assetTabFromSlug(normalized);
    if (!tab) return;
    setAssetTab(tab);
    setFocusSlug(normalized);
    const nextHash = `#${normalized}`;
    if (window.location.hash !== nextHash) {
      window.history.pushState(null, '', nextHash);
    }
    requestAnimationFrame(() => {
      document.getElementById('market-charts')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }, []);

  useEffect(() => {
    const onHashChange = () => {
      const slug = slugFromHash(window.location.hash);
      setFocusSlug(slug);
      if (slug) setAssetTab(assetTabFromSlug(slug) ?? 'BTC');
    };
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('popstate', onHashChange);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
      window.removeEventListener('popstate', onHashChange);
    };
  }, []);

  const feeds = snapshot?.feeds ?? {};
  const activeFeeds = displayFeeds.filter((id) => feeds[id]);

  return (
    <div className="app viewport">
      <header className="header compact">
        <div>
          <h1>Beat Spike</h1>
          <p className="subtitle">
            Beat-cross spikes · {chartInterval} · signal feed {priceFeed}
          </p>
        </div>
        <div className="status-bar">
          <span className={`conn ${connected ? 'conn-on' : 'conn-off'}`}>
            {connected ? 'Live' : 'Offline'}
          </span>
          {snapshot?.updated_at && (
            <span className="updated">{new Date(snapshot.updated_at).toLocaleTimeString()}</span>
          )}
          {error && <span className="err">{error}</span>}
        </div>
      </header>
      <BotSlugs focusSlug={focusSlug} onSlugClick={navigateToSlug} />

      <nav className="asset-tabs" aria-label="Asset">
        {assets.map((asset) => (
          <button
            key={asset}
            type="button"
            className={`asset-tab ${assetTab === asset ? 'asset-tab-active' : ''}`}
            onClick={() => setAssetTab(asset)}
            aria-pressed={assetTab === asset}
          >
            {asset}
          </button>
        ))}
      </nav>

      <DeltaStatusBar snapshot={snapshot} activeAsset={assetTab} priceFeed={priceFeed} />

      {intervals.length > 1 && (
        <nav className="asset-tabs interval-tabs" aria-label="Interval">
          {intervals.map((iv) => (
            <span key={iv} className="interval-pill">
              {iv}
            </span>
          ))}
        </nav>
      )}

      <main id="market-charts" className="cex-board">
        {activeFeeds.map((feedId) => {
          const feed = feeds[feedId];
          const row = feed.assets[assetTab];
          const slugMatchesCurrent = intervalBeat?.slug === activeSlug;
          const feedBeat =
            slugFeedBeats?.[feedId] != null && Number.isFinite(Number(slugFeedBeats[feedId]))
              ? Number(slugFeedBeats[feedId])
              : slugMatchesCurrent
                ? row?.beat ?? null
                : null;
          const beatDeltaUsd =
            row?.price != null && feedBeat != null
              ? row.price - feedBeat
              : row?.delta ?? null;
          return (
            <FeedStrip
              key={`${assetTab}-${feedId}`}
              asset={assetTab}
              feedId={feedId}
              feedLabel={feed.label}
              row={row}
              healthState={feed.health.state}
              healthStale={feed.health.data_stale}
              storeRef={storeRef}
              tick={tick}
              highlight={feedId === priceFeed}
              beatPrice={feedBeat}
              beatSlug={activeSlug}
              beatInterval={chartInterval}
              beatDeltaUsd={beatDeltaUsd}
            />
          );
        })}
        {activeFeeds.length === 0 && (
          <p className="cex-board-empty">Waiting for price feeds… (run make server)</p>
        )}
      </main>

      <TradeHistory focusSlug={focusSlug} onSlugClick={navigateToSlug} />
      <MonitorHistory />
    </div>
  );
}
