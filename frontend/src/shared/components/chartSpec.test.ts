import { describe, expect, it } from 'vitest';

import { ChartSpecError, isChartLanguage, parseChartSpec } from './chartSpec';

describe('chartSpec', () => {
  it('recognises the fences that mean "draw this"', () => {
    expect(isChartLanguage('chart')).toBe(true);
    expect(isChartLanguage('Graph')).toBe(true);
    expect(isChartLanguage('python')).toBe(false);
  });

  it('reads the canonical labels-and-series shape', () => {
    const spec = parseChartSpec(
      JSON.stringify({
        type: 'line',
        title: 'Citations',
        labels: ['2021', '2022'],
        series: [{ name: 'Ours', data: [3, 8], color: '#f00' }],
        yLabel: 'Count',
      }),
    );

    expect(spec.kind).toBe('line');
    expect(spec.title).toBe('Citations');
    expect(spec.labels).toEqual(['2021', '2022']);
    expect(spec.series).toHaveLength(1);
    expect(spec.series[0]).toMatchObject({ name: 'Ours', values: [3, 8], color: '#f00' });
    expect(spec.yLabel).toBe('Count');
  });

  it('accepts the flat label/value rows a model reaches for first', () => {
    const spec = parseChartSpec(
      JSON.stringify({ type: 'pie', data: [{ label: 'A', value: 2 }, { name: 'B', y: 5 }] }),
    );

    expect(spec.labels).toEqual(['A', 'B']);
    expect(spec.series[0].values).toEqual([2, 5]);
  });

  it('accepts a bare number array and invents positional labels', () => {
    const spec = parseChartSpec(JSON.stringify({ type: 'bar', series: [1, 2, 3] }));

    expect(spec.labels).toEqual(['1', '2', '3']);
    expect(spec.series[0].values).toEqual([1, 2, 3]);
  });

  it('reads scatter points from pairs and from objects', () => {
    const spec = parseChartSpec(
      JSON.stringify({
        type: 'scatter',
        series: [
          { name: 'runs', data: [[1, 2], { x: 3, y: 4, label: 'peak' }] },
        ],
      }),
    );

    expect(spec.series[0].points).toEqual([
      { x: 1, y: 2 },
      { x: 3, y: 4, label: 'peak' },
    ]);
    expect(spec.labels).toEqual([]);
  });

  it('keeps gaps in a series as gaps rather than zeros', () => {
    const spec = parseChartSpec(
      JSON.stringify({ type: 'line', labels: ['a', 'b', 'c'], series: [{ name: 's', data: [1, null, 3] }] }),
    );

    expect(spec.series[0].values).toEqual([1, null, 3]);
  });

  it('reads numeric strings, since JSON from a model often quotes them', () => {
    const spec = parseChartSpec(
      JSON.stringify({ type: 'bar', labels: ['a'], series: [{ name: 's', data: ['4.5'] }] }),
    );

    expect(spec.series[0].values).toEqual([4.5]);
  });

  it('explains what is wrong instead of throwing something opaque', () => {
    expect(() => parseChartSpec('{not json')).toThrow(ChartSpecError);
    expect(() => parseChartSpec('[1,2]')).toThrow(/JSON object/);
    expect(() => parseChartSpec('{"type":"sankey","data":[]}')).toThrow(/Unsupported chart type/);
    expect(() => parseChartSpec('{"type":"bar","labels":["a"]}')).toThrow(/no series/);
  });
});
