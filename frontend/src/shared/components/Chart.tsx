import { useMemo, useState } from 'react';
import { Icon } from './Icons';
import {
  ChartSpecError,
  parseChartSpec,
  type ChartSeries,
  type ChartSpec,
} from './chartSpec';

/**
 * Charts for analysis answers.
 *
 * Drawn as plain SVG against the theme's `--viz-*` tokens: no charting
 * dependency, no canvas, and every mark inherits the active palette. The
 * viewBox is fixed and the element scales, so one geometry works from a chat
 * column to a full-width panel.
 */

const W = 760;
const H = 380;
const PAD = { top: 22, right: 20, bottom: 52, left: 62 };
const PLOT = { w: W - PAD.left - PAD.right, h: H - PAD.top - PAD.bottom };
const SERIES_COLORS = ['var(--viz-1)', 'var(--viz-2)', 'var(--viz-3)', 'var(--viz-4)', 'var(--viz-5)', 'var(--viz-6)'];

export function ChartBlock({ source }: { source: string }) {
  const parsed = useMemo(() => {
    try {
      return { spec: parseChartSpec(source), error: null as string | null };
    } catch (error) {
      return {
        spec: null,
        error: error instanceof ChartSpecError ? error.message : 'The chart could not be read.',
      };
    }
  }, [source]);

  if (!parsed.spec) {
    return (
      <div className="chart-card chart-card-error">
        <div className="chart-head">
          <Icon name="runs" size={15} />
          <strong>Chart could not be rendered</strong>
        </div>
        <p className="chart-error-message">{parsed.error}</p>
        <pre className="code-block">{source}</pre>
      </div>
    );
  }

  return <Chart spec={parsed.spec} source={source} />;
}

export function Chart({ spec, source }: { spec: ChartSpec; source?: string }) {
  const [showData, setShowData] = useState(false);
  const multi = spec.series.length > 1;

  return (
    <figure className="chart-card" data-kind={spec.kind}>
      <div className="chart-head">
        <Icon name="runs" size={15} className="chart-head-icon" />
        <strong>{spec.title ?? 'Chart'}</strong>
        <span className="spacer" />
        <button
          type="button"
          className={showData ? 'active' : undefined}
          aria-pressed={showData}
          title={showData ? 'Hide the underlying numbers' : 'Show the underlying numbers'}
          onClick={() => setShowData((value) => !value)}
        >
          <Icon name="papers" size={14} />
        </button>
        <button type="button" title="Download the chart data" aria-label="Download chart data" onClick={() => download(spec, source)}>
          <Icon name="download" size={14} />
        </button>
      </div>

      <div className="chart-plot">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={spec.title ?? `${spec.kind} chart`} preserveAspectRatio="xMidYMid meet">
          {spec.kind === 'pie' || spec.kind === 'donut' ? (
            <PieChart spec={spec} />
          ) : spec.kind === 'scatter' ? (
            <ScatterChart spec={spec} />
          ) : spec.kind === 'bar' ? (
            <BarChart spec={spec} />
          ) : (
            <LineChart spec={spec} />
          )}
        </svg>
      </div>

      {multi || spec.kind === 'pie' || spec.kind === 'donut' ? <Legend spec={spec} /> : null}
      {showData ? <DataTable spec={spec} /> : null}
      {spec.title ? <figcaption className="chart-caption">{spec.title}</figcaption> : null}
    </figure>
  );
}

/* ------------------------------------------------------------- Cartesian --- */

function BarChart({ spec }: { spec: ChartSpec }) {
  const { min, max, ticks } = scaleFor(spec);
  const groups = spec.labels.length;
  const groupWidth = PLOT.w / Math.max(groups, 1);
  const inner = groupWidth * 0.7;
  const barWidth = spec.stacked ? inner : inner / spec.series.length;
  const y = (value: number) => PAD.top + PLOT.h - ((value - min) / (max - min || 1)) * PLOT.h;

  return (
    <>
      <Grid ticks={ticks} y={y} spec={spec} />
      <CategoryAxis labels={spec.labels} groupWidth={groupWidth} />
      {spec.labels.map((_, index) => {
        const left = PAD.left + index * groupWidth + (groupWidth - inner) / 2;
        let stackTop = 0;
        return spec.series.map((series, seriesIndex) => {
          const value = series.values[index];
          if (value === null || value === undefined) return null;
          const color = colorFor(series, seriesIndex);
          const base = spec.stacked ? stackTop : Math.max(min, 0);
          const top = spec.stacked ? stackTop + value : value;
          if (spec.stacked) stackTop = top;
          const yTop = y(Math.max(base, top));
          const height = Math.abs(y(base) - y(top));
          const x = spec.stacked ? left : left + seriesIndex * barWidth;
          return (
            <g key={`${index}-${seriesIndex}`} className="chart-bar">
              <rect
                x={x}
                y={yTop}
                width={Math.max(barWidth - 2, 1)}
                height={Math.max(height, 1)}
                rx={3}
                fill={color}
              >
                <title>{`${series.name} · ${spec.labels[index]}: ${format(value, spec.unit)}`}</title>
              </rect>
              {spec.showValues && !spec.stacked ? (
                <text className="chart-value" x={x + barWidth / 2} y={yTop - 5} textAnchor="middle">
                  {format(value, spec.unit)}
                </text>
              ) : null}
            </g>
          );
        });
      })}
      <AxisTitles spec={spec} />
    </>
  );
}

function LineChart({ spec }: { spec: ChartSpec }) {
  const { min, max, ticks } = scaleFor(spec);
  const step = spec.labels.length > 1 ? PLOT.w / (spec.labels.length - 1) : 0;
  const x = (index: number) => (spec.labels.length > 1 ? PAD.left + index * step : PAD.left + PLOT.w / 2);
  const y = (value: number) => PAD.top + PLOT.h - ((value - min) / (max - min || 1)) * PLOT.h;

  return (
    <>
      <Grid ticks={ticks} y={y} spec={spec} />
      <CategoryAxis labels={spec.labels} groupWidth={step} offset={spec.labels.length > 1 ? -step / 2 : 0} />
      {spec.series.map((series, seriesIndex) => {
        const color = colorFor(series, seriesIndex);
        const points = series.values
          .map((value, index) => (value === null ? null : { x: x(index), y: y(value), value, index }))
          .filter((point): point is { x: number; y: number; value: number; index: number } => point !== null);
        if (!points.length) return null;
        const path = points.map((point, index) => `${index ? 'L' : 'M'}${point.x} ${point.y}`).join(' ');
        const areaPath = `${path} L${points[points.length - 1].x} ${y(Math.max(min, 0))} L${points[0].x} ${y(Math.max(min, 0))} Z`;
        return (
          <g key={series.name + seriesIndex}>
            {spec.kind === 'area' ? <path d={areaPath} fill={color} opacity={0.16} /> : null}
            <path d={path} fill="none" stroke={color} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" />
            {points.map((point) => (
              <circle key={point.index} cx={point.x} cy={point.y} r={3.6} fill={color} stroke="var(--surface)" strokeWidth={1.4}>
                <title>{`${series.name} · ${spec.labels[point.index]}: ${format(point.value, spec.unit)}`}</title>
              </circle>
            ))}
          </g>
        );
      })}
      <AxisTitles spec={spec} />
    </>
  );
}

function ScatterChart({ spec }: { spec: ChartSpec }) {
  const xs = spec.series.flatMap((series) => series.points.map((point) => point.x));
  const ys = spec.series.flatMap((series) => series.points.map((point) => point.y));
  const xScale = niceScale(Math.min(...xs), Math.max(...xs));
  const yScale = niceScale(Math.min(...ys), Math.max(...ys));
  const px = (value: number) => PAD.left + ((value - xScale.min) / (xScale.max - xScale.min || 1)) * PLOT.w;
  const py = (value: number) => PAD.top + PLOT.h - ((value - yScale.min) / (yScale.max - yScale.min || 1)) * PLOT.h;

  return (
    <>
      <Grid ticks={yScale.ticks} y={py} spec={spec} />
      {xScale.ticks.map((tick) => (
        <text key={tick} className="chart-tick" x={px(tick)} y={PAD.top + PLOT.h + 18} textAnchor="middle">
          {format(tick)}
        </text>
      ))}
      {spec.series.map((series, seriesIndex) => {
        const color = colorFor(series, seriesIndex);
        return (
          <g key={series.name + seriesIndex}>
            {series.points.map((point, index) => (
              <circle key={index} cx={px(point.x)} cy={py(point.y)} r={5} fill={color} opacity={0.78}>
                <title>{`${point.label ?? series.name}: ${format(point.x)}, ${format(point.y, spec.unit)}`}</title>
              </circle>
            ))}
          </g>
        );
      })}
      <AxisTitles spec={spec} />
    </>
  );
}

/* ------------------------------------------------------------------ Pie --- */

function PieChart({ spec }: { spec: ChartSpec }) {
  const series = spec.series[0];
  const values = series.values.map((value) => Math.max(value ?? 0, 0));
  const total = values.reduce((sum, value) => sum + value, 0);
  const cx = W / 2;
  const cy = PAD.top + PLOT.h / 2;
  const radius = Math.min(PLOT.h, PLOT.w) / 2 - 6;
  const hole = spec.kind === 'donut' ? radius * 0.58 : 0;
  let angle = -Math.PI / 2;

  if (!total) return <text className="chart-tick" x={cx} y={cy} textAnchor="middle">No data</text>;

  return (
    <>
      {values.map((value, index) => {
        const sweep = (value / total) * Math.PI * 2;
        const path = arcPath(cx, cy, radius, hole, angle, angle + sweep);
        const mid = angle + sweep / 2;
        angle += sweep;
        const share = (value / total) * 100;
        const labelRadius = hole ? (radius + hole) / 2 : radius * 0.68;
        return (
          <g key={index}>
            <path d={path} fill={SERIES_COLORS[index % SERIES_COLORS.length]} stroke="var(--surface)" strokeWidth={1.5}>
              <title>{`${spec.labels[index] ?? `Slice ${index + 1}`}: ${format(value, spec.unit)} (${share.toFixed(1)}%)`}</title>
            </path>
            {share >= 6 ? (
              <text
                className="chart-slice-label"
                x={cx + Math.cos(mid) * labelRadius}
                y={cy + Math.sin(mid) * labelRadius}
                textAnchor="middle"
                dominantBaseline="middle"
              >
                {`${Math.round(share)}%`}
              </text>
            ) : null}
          </g>
        );
      })}
      {hole ? (
        <text className="chart-donut-total" x={cx} y={cy} textAnchor="middle" dominantBaseline="middle">
          {format(total, spec.unit)}
        </text>
      ) : null}
    </>
  );
}

function arcPath(cx: number, cy: number, radius: number, hole: number, from: number, to: number): string {
  const large = to - from > Math.PI ? 1 : 0;
  const x1 = cx + Math.cos(from) * radius;
  const y1 = cy + Math.sin(from) * radius;
  const x2 = cx + Math.cos(to) * radius;
  const y2 = cy + Math.sin(to) * radius;
  if (!hole) return `M${cx} ${cy} L${x1} ${y1} A${radius} ${radius} 0 ${large} 1 ${x2} ${y2} Z`;
  const x3 = cx + Math.cos(to) * hole;
  const y3 = cy + Math.sin(to) * hole;
  const x4 = cx + Math.cos(from) * hole;
  const y4 = cy + Math.sin(from) * hole;
  return `M${x1} ${y1} A${radius} ${radius} 0 ${large} 1 ${x2} ${y2} L${x3} ${y3} A${hole} ${hole} 0 ${large} 0 ${x4} ${y4} Z`;
}

/* ------------------------------------------------------------- Furniture --- */

function Grid({ ticks, y, spec }: { ticks: number[]; y: (value: number) => number; spec: ChartSpec }) {
  return (
    <>
      {ticks.map((tick) => (
        <g key={tick}>
          <line className="chart-grid" x1={PAD.left} x2={PAD.left + PLOT.w} y1={y(tick)} y2={y(tick)} />
          <text className="chart-tick" x={PAD.left - 10} y={y(tick)} textAnchor="end" dominantBaseline="middle">
            {format(tick, spec.unit)}
          </text>
        </g>
      ))}
      <line className="chart-axis" x1={PAD.left} x2={PAD.left + PLOT.w} y1={PAD.top + PLOT.h} y2={PAD.top + PLOT.h} />
    </>
  );
}

function CategoryAxis({ labels, groupWidth, offset = 0 }: { labels: string[]; groupWidth: number; offset?: number }) {
  // Dense axes become unreadable long before they become useful: thin them out.
  const stride = Math.ceil(labels.length / 14);
  return (
    <>
      {labels.map((label, index) =>
        index % stride === 0 ? (
          <text
            key={`${label}-${index}`}
            className="chart-tick"
            x={PAD.left + index * groupWidth + groupWidth / 2 + offset}
            y={PAD.top + PLOT.h + 20}
            textAnchor="middle"
          >
            {label.length > 14 ? `${label.slice(0, 13)}…` : label}
          </text>
        ) : null,
      )}
    </>
  );
}

function AxisTitles({ spec }: { spec: ChartSpec }) {
  return (
    <>
      {spec.xLabel ? (
        <text className="chart-axis-title" x={PAD.left + PLOT.w / 2} y={H - 8} textAnchor="middle">
          {spec.xLabel}
        </text>
      ) : null}
      {spec.yLabel ? (
        <text
          className="chart-axis-title"
          transform={`translate(14 ${PAD.top + PLOT.h / 2}) rotate(-90)`}
          textAnchor="middle"
        >
          {spec.yLabel}
        </text>
      ) : null}
    </>
  );
}

function Legend({ spec }: { spec: ChartSpec }) {
  const entries =
    spec.kind === 'pie' || spec.kind === 'donut'
      ? spec.labels.map((label, index) => ({ label, color: SERIES_COLORS[index % SERIES_COLORS.length] }))
      : spec.series.map((series, index) => ({ label: series.name, color: colorFor(series, index) }));

  return (
    <ul className="chart-legend">
      {entries.map((entry, index) => (
        <li key={`${entry.label}-${index}`}>
          <span className="chart-legend-dot" style={{ background: entry.color }} />
          {entry.label}
        </li>
      ))}
    </ul>
  );
}

function DataTable({ spec }: { spec: ChartSpec }) {
  if (spec.kind === 'scatter') {
    return (
      <div className="chart-data">
        <table>
          <thead>
            <tr>
              <th>Series</th>
              <th>x</th>
              <th>y</th>
            </tr>
          </thead>
          <tbody>
            {spec.series.flatMap((series, seriesIndex) =>
              series.points.map((point, index) => (
                <tr key={`${seriesIndex}-${index}`}>
                  <td>{series.name}</td>
                  <td>{format(point.x)}</td>
                  <td>{format(point.y, spec.unit)}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className="chart-data">
      <table>
        <thead>
          <tr>
            <th>{spec.xLabel ?? 'Label'}</th>
            {spec.series.map((series, index) => (
              <th key={`${series.name}-${index}`}>{series.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {spec.labels.map((label, index) => (
            <tr key={`${label}-${index}`}>
              <td>{label}</td>
              {spec.series.map((series, seriesIndex) => (
                <td key={seriesIndex}>{series.values[index] === null || series.values[index] === undefined ? '—' : format(series.values[index] as number, spec.unit)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ----------------------------------------------------------------- Maths --- */

function scaleFor(spec: ChartSpec): { min: number; max: number; ticks: number[] } {
  const totals: number[] = [];
  if (spec.stacked) {
    spec.labels.forEach((_, index) => {
      totals.push(spec.series.reduce((sum, series) => sum + (series.values[index] ?? 0), 0));
    });
  } else {
    spec.series.forEach((series) => series.values.forEach((value) => value !== null && totals.push(value)));
  }
  const min = Math.min(0, ...totals);
  const max = Math.max(...totals, 0);
  return niceScale(min, max);
}

function niceScale(rawMin: number, rawMax: number): { min: number; max: number; ticks: number[] } {
  let min = Number.isFinite(rawMin) ? rawMin : 0;
  let max = Number.isFinite(rawMax) ? rawMax : 1;
  if (min === max) {
    max = max === 0 ? 1 : max * 1.2;
    min = Math.min(0, min);
  }
  const step = niceStep((max - min) / 4);
  min = Math.floor(min / step) * step;
  max = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let tick = min; tick <= max + step / 2; tick += step) ticks.push(round(tick));
  return { min, max, ticks };
}

function niceStep(rough: number): number {
  const exponent = Math.floor(Math.log10(Math.abs(rough) || 1));
  const magnitude = 10 ** exponent;
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function round(value: number): number {
  return Math.abs(value) < 1e-9 ? 0 : Number(value.toPrecision(12));
}

function format(value: number, unit?: string): string {
  const abs = Math.abs(value);
  let text: string;
  if (abs >= 1_000_000_000) text = `${round(value / 1_000_000_000)}B`;
  else if (abs >= 1_000_000) text = `${round(value / 1_000_000)}M`;
  else if (abs >= 10_000) text = `${round(value / 1000)}k`;
  else text = String(round(Number(value.toFixed(4))));
  return unit ? `${text}${unit}` : text;
}

function colorFor(series: ChartSeries, index: number): string {
  return series.color ?? SERIES_COLORS[index % SERIES_COLORS.length];
}

function download(spec: ChartSpec, source?: string) {
  const payload = source ?? JSON.stringify(spec, null, 2);
  const url = URL.createObjectURL(new Blob([payload], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${(spec.title ?? 'chart').replace(/\W+/g, '-').toLowerCase()}.json`;
  link.click();
  URL.revokeObjectURL(url);
}
