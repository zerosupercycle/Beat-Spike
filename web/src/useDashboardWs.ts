import { useEffect, useRef, useState } from 'react';
import { ingestSnapshot, type HistoryStore } from './history';
import type { DashboardSnapshot } from './types';

function wsUrl(): string {
  const base = import.meta.env.VITE_WS_URL as string | undefined;
  if (base) return base.replace(/\/$/, '');
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (import.meta.env.DEV) return `${proto}//${window.location.host}/ws`;
  return `${proto}//${window.location.hostname}:8788/ws`;
}

export function useDashboardWs() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const storeRef = useRef<HistoryStore>(new Map());
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (cancelled) return;
      setError(null);
      ws = new WebSocket(wsUrl());

      ws.onopen = () => {
        if (!cancelled) setConnected(true);
      };

      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data as string) as DashboardSnapshot;
          if (cancelled) return;
          ingestSnapshot(storeRef.current, data);
          setTick((n) => n + 1);
          setSnapshot(data);
        } catch {
          /* ignore */
        }
      };

      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) {
          setError('Disconnected — reconnecting…');
          retryTimer = setTimeout(connect, 2000);
        }
      };

      ws.onerror = () => setConnected(false);
    };

    connect();

    return () => {
      cancelled = true;
      clearTimeout(retryTimer);
      ws?.close();
    };
  }, []);

  return { snapshot, connected, error, storeRef, tick };
}
