import { useEffect, useState } from 'react';

function duration(seconds: number) {
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)}h ${Math.ceil((seconds % 3600) / 60)} min`;
}

type ProgressSample = {
  count: number;
  baseline: number;
  started: number;
  running: boolean;
  windowSeconds: number | undefined;
  completions: { count: number; time: number }[];
};

export function ProgressTiming({ count, total, running, unit = 'papers', windowSeconds }: {
  count: number;
  total: number | null;
  running: boolean;
  unit?: 'papers' | 'pages';
  windowSeconds?: number;
}) {
  const [sample, setSample] = useState<ProgressSample>(() => ({
    count, baseline: count, started: performance.now(), running, windowSeconds, completions: [],
  }));
  const [now, setNow] = useState(() => performance.now());

  useEffect(() => {
    const time = performance.now();
    setNow(time);
    setSample((previous) => {
      if (count < previous.count || running !== previous.running || windowSeconds !== previous.windowSeconds) {
        return { count, baseline: count, started: time, running, windowSeconds, completions: [] };
      }
      if (count === previous.count) return previous;
      return {
        ...previous, count,
        completions: windowSeconds === undefined ? [] : [
          ...previous.completions.filter((completion) => completion.time > time - windowSeconds * 1000),
          { count: count - previous.count, time },
        ],
      };
    });
  }, [count, running, windowSeconds]);

  useEffect(() => {
    if (!running || (total !== null && count >= total)) return;
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, count, total]);

  if (total !== null && count >= total) return <p className="field-hint">Complete</p>;
  if (!running) return <p className="field-hint">Not running; time remaining unavailable.</p>;
  const elapsed = Math.max(0, (now - sample.started) / 1000);
  const seconds = windowSeconds === undefined ? elapsed : Math.min(elapsed, windowSeconds);
  const completed = windowSeconds === undefined ? count - sample.baseline
    : sample.completions.reduce((sum, completion) =>
      completion.time > now - windowSeconds * 1000 ? sum + completion.count : sum, 0);
  const rate = seconds > 0 && completed > 0 ? completed / seconds : null;
  const waitingMessage = windowSeconds !== undefined && elapsed >= windowSeconds
    ? `No ${unit} completed in the last ${duration(windowSeconds)}; time remaining unavailable.`
    : 'Measuring speed; time remaining available after more progress.';

  return <p className="field-hint">
    Observed for {duration(seconds)} · {rate === null
      ? waitingMessage
      : <>
        {(rate * 60).toFixed(1)} {unit}/min ({rate.toFixed(2)} {unit}/s)
        {unit === 'pages' ? ` · ${(1 / rate).toFixed(1)} s/page` : ''}
        {total === null ? ' · time remaining unavailable until the page total is known.'
          : ` · ~${duration((total - count) / rate)} remaining`}
      </>}
    <br />Estimate for this task only, based on completions {windowSeconds === undefined
      ? 'while this page is open' : `in the last ${duration(windowSeconds)} while this page is open`}; includes waiting time.
    {unit === 'papers' ? 'Papers can vary in size. Paper throughput is not model-request RPM/RPS.'
      : 'Pages can vary in size. Page throughput is not model-request RPM/RPS.'}
  </p>;
}
