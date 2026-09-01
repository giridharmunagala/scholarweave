/**
 * Parsing for the ```chart fences the agent can emit.
 *
 * The agent writes JSON, and JSON written by a model is rarely exactly the
 * shape you asked for. This module accepts the handful of shapes that mean the
 * same thing — a flat `data` array, `labels` vs `x`, a bare number array for a
 * single series — and normalises them into one spec the renderer can trust.
 */

export type ChartKind = 'bar' | 'column' | 'line' | 'area' | 'pie' | 'donut' | 'scatter';

export interface ChartSeries {
  name: string;
  /** Category charts: one value per label. Missing points are null. */
  values: Array<number | null>;
  /** Scatter charts: explicit points. */
  points: Array<{ x: number; y: number; label?: string }>;
  color?: string;
}

export interface ChartSpec {
  kind: ChartKind;
  title?: string;
  labels: string[];
  series: ChartSeries[];
  stacked: boolean;
  horizontal: boolean;
  xLabel?: string;
  yLabel?: string;
  /** Renders raw values on the marks; useful for small comparison charts. */
  showValues: boolean;
  unit?: string;
}

const KINDS = new Set<ChartKind>(['bar', 'column', 'line', 'area', 'pie', 'donut', 'scatter']);

export const CHART_LANGUAGES = new Set(['chart', 'graph', 'plot', 'viz', 'scholarweave-chart']);

export function isChartLanguage(language: string): boolean {
  return CHART_LANGUAGES.has(language.trim().toLowerCase());
}

export class ChartSpecError extends Error {}

export function parseChartSpec(source: string): ChartSpec {
  let raw: unknown;
  try {
    raw = JSON.parse(source);
  } catch {
    throw new ChartSpecError('The chart block is not valid JSON.');
  }
  return normalizeChartSpec(raw);
}

export function normalizeChartSpec(raw: unknown): ChartSpec {
  if (!isRecord(raw)) throw new ChartSpecError('A chart must be a JSON object.');

  const kind = readKind(raw.type ?? raw.kind ?? raw.chart);
  const labels = readLabels(raw);
  const series = readSeries(raw, labels.length, kind);
  if (!series.length) throw new ChartSpecError('The chart has no series to draw.');

  const filled = labels.length
    ? labels
    : series[0].values.map((_, index) => String(index + 1));

  return {
    kind,
    title: optionalString(raw.title),
    labels: kind === 'scatter' ? [] : filled,
    series,
    stacked: raw.stacked === true,
    horizontal: raw.horizontal === true || raw.orientation === 'horizontal',
    xLabel: optionalString(raw.xLabel ?? raw.x_label ?? axisLabel(raw.xAxis) ?? axisLabel(raw.x_axis)),
    yLabel: optionalString(raw.yLabel ?? raw.y_label ?? axisLabel(raw.yAxis) ?? axisLabel(raw.y_axis)),
    showValues: raw.showValues === true || raw.values === true || raw.labelsOnMarks === true,
    unit: optionalString(raw.unit ?? raw.suffix),
  };
}

function readKind(value: unknown): ChartKind {
  const id = typeof value === 'string' ? value.trim().toLowerCase() : 'bar';
  if (KINDS.has(id as ChartKind)) return id === 'column' ? 'bar' : (id as ChartKind);
  throw new ChartSpecError(
    `Unsupported chart type "${String(value)}". Use bar, line, area, pie, donut or scatter.`,
  );
}

function readLabels(raw: Record<string, unknown>): string[] {
  const candidate = raw.labels ?? raw.x ?? raw.categories ?? axisData(raw.xAxis) ?? axisData(raw.x_axis);
  if (Array.isArray(candidate)) return candidate.map((item) => String(item));

  // Flat `data: [{ label, value }]` is the shape models reach for most often.
  const flat = flatRows(raw);
  if (flat) return flat.map((row) => row.label);
  return [];
}

function readSeries(raw: Record<string, unknown>, labelCount: number, kind: ChartKind): ChartSeries[] {
  const declared = raw.series ?? raw.datasets ?? raw.y;

  if (Array.isArray(declared) && declared.length && isRecord(declared[0])) {
    return declared
      .map((entry, index) => toSeries(entry as Record<string, unknown>, index, kind))
      .filter((entry): entry is ChartSeries => entry !== null);
  }

  if (Array.isArray(declared) && declared.every(isNumeric)) {
    return [emptySeries('Value', declared.map(toNumber))];
  }

  const flat = flatRows(raw);
  if (flat) return [{ ...emptySeries('Value', flat.map((row) => row.value)), color: undefined }];

  if (Array.isArray(raw.values) && raw.values.every(isNumeric)) {
    return [emptySeries('Value', raw.values.map(toNumber))];
  }

  if (labelCount && Array.isArray(raw.data) && raw.data.every(isNumeric)) {
    return [emptySeries('Value', raw.data.map(toNumber))];
  }

  return [];
}

function toSeries(entry: Record<string, unknown>, index: number, kind: ChartKind): ChartSeries | null {
  const name = optionalString(entry.name ?? entry.label ?? entry.title) ?? `Series ${index + 1}`;
  const color = optionalString(entry.color ?? entry.colour);
  const data = entry.data ?? entry.values ?? entry.points ?? entry.y;

  if (kind === 'scatter') {
    const points = Array.isArray(data)
      ? data
          .map((point) => {
            if (Array.isArray(point) && point.length >= 2) {
              return { x: toNumber(point[0]), y: toNumber(point[1]) };
            }
            if (isRecord(point) && isNumeric(point.x) && isNumeric(point.y)) {
              return { x: toNumber(point.x), y: toNumber(point.y), label: optionalString(point.label) };
            }
            return null;
          })
          .filter((point): point is { x: number; y: number; label?: string } => point !== null)
      : [];
    if (!points.length) return null;
    return { name, values: [], points, color };
  }

  if (!Array.isArray(data)) return null;
  const values = data.map((value) => (isNumeric(value) ? toNumber(value) : null));
  if (!values.length) return null;
  return { name, values, points: [], color };
}

function flatRows(raw: Record<string, unknown>): Array<{ label: string; value: number }> | null {
  const data = raw.data ?? raw.rows ?? raw.items;
  if (!Array.isArray(data) || !data.length || !isRecord(data[0])) return null;
  const rows = data
    .map((row) => {
      if (!isRecord(row)) return null;
      const label = optionalString(row.label ?? row.name ?? row.category ?? row.x);
      const value = row.value ?? row.y ?? row.count ?? row.total;
      if (label === undefined || !isNumeric(value)) return null;
      return { label, value: toNumber(value) };
    })
    .filter((row): row is { label: string; value: number } => row !== null);
  return rows.length ? rows : null;
}

function emptySeries(name: string, values: Array<number | null>): ChartSeries {
  return { name, values, points: [] };
}

function axisData(value: unknown): unknown {
  return isRecord(value) ? value.data ?? value.categories ?? value.labels : undefined;
}

function axisLabel(value: unknown): string | undefined {
  return isRecord(value) ? optionalString(value.label ?? value.title) : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNumeric(value: unknown): boolean {
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value === 'string' && value.trim()) return Number.isFinite(Number(value));
  return false;
}

function toNumber(value: unknown): number {
  return typeof value === 'number' ? value : Number(value);
}

function optionalString(value: unknown): string | undefined {
  if (typeof value === 'string' && value.trim()) return value.trim();
  if (typeof value === 'number') return String(value);
  return undefined;
}
