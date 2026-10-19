import { useEffect, useState } from 'react';

function duration(seconds: number) {
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)}h ${Math.ceil((seconds % 3600) / 60)} min`;
}

export function ProgressTiming({ count, total, running, unit = 'papers' }: {
  count: number;
  total: number;
  running: boolean;
  unit?: 'papers' | 'pages';
}) {
  const [sample, setSample] = useState(() => ({
    count, baseline: count, started: performance.now(), running,
  }));
  const [now, setNow] = useState(() => performance.now());

  useEffect(() => {
    const time = performance.now();
    setNow(time);
    setSample((previous) => count < previous.count || running !== previous.running
      ? { count, baseline: count, started: time, running }
      : { ...previous, count });
  }, [count, running]);

  useEffect(() => {
    if (!running || count >= total) return;
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, count, total]);

  if (count >= total) return <p className="field-hint">Complete</p>;
  if (!running) return <p className="field-hint">Not running; time remaining unavailable.</p>;
  const seconds = Math.max(0, (now - sample.started) / 1000);
  const completed = count - sample.baseline;
  const rate = seconds > 0 && completed > 0 ? completed / seconds : null;

  return <p className="field-hint">
    Observed for {duration(seconds)} · {rate === null
      ? 'Measuring speed; time remaining available after more progress.'
      : `${(rate * 60).toFixed(1)} ${unit}/min (${rate.toFixed(2)} ${unit}/s) · ~${duration((total - count) / rate)} remaining`}
    <br />Estimate for this task only, based on completions while this page is open; includes waiting time.
    {unit === 'papers' ? 'Papers can vary in size. Paper throughput is not model-request RPM/RPS.'
      : 'Pages can vary in size. Page throughput is not model-request RPM/RPS.'}
  </p>;
}
