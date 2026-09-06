import { useEffect, useState } from 'react';

interface Rates {
  promptRate: number | null;
  generationRate: number | null;
}

interface DisplayedRates extends Rates {
  updatedAt: number;
}

const UPDATE_INTERVAL_MS = 5_000;

// Keep the cache above message components so completion/reconciliation cannot bypass the throttle.
export function useThrottledRates<T extends Rates>(metrics: ReadonlyMap<string, T>): Map<string, T> {
  const [displayed, setDisplayed] = useState(new Map<string, DisplayedRates>());

  useEffect(() => {
    const now = Date.now();
    const next = new Map(displayed);
    let changed = false;
    let delay = Infinity;
    for (const [id, rates] of metrics) {
      const previous = displayed.get(id);
      if (previous?.promptRate === rates.promptRate
        && previous?.generationRate === rates.generationRate) continue;
      const remaining = previous ? UPDATE_INTERVAL_MS - (now - previous.updatedAt) : 0;
      if (remaining <= 0) {
        next.set(id, {
          promptRate: rates.promptRate,
          generationRate: rates.generationRate,
          updatedAt: now,
        });
        changed = true;
      } else {
        delay = Math.min(delay, remaining);
      }
    }
    if (changed) setDisplayed(next);
    if (!Number.isFinite(delay)) return;
    const timer = window.setTimeout(() => setDisplayed((current) => new Map(current)), delay);
    return () => window.clearTimeout(timer);
  }, [metrics, displayed]);

  return new Map([...metrics].map(([id, value]) => {
    const rates = displayed.get(id);
    return [id, rates ? {
      ...value,
      promptRate: rates.promptRate,
      generationRate: rates.generationRate,
    } : value];
  }));
}
