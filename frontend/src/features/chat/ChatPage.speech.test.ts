import { describe, expect, it } from 'vitest';

import {
  encodePcm16,
  encodeWav,
  PROVIDER_TRANSCRIPTION_INTERVAL_MS,
} from './ChatPage';

describe('speech recording', () => {
  it('requests live transcript updates without a multi-second client delay', () => {
    expect(PROVIDER_TRANSCRIPTION_INTERVAL_MS).toBeLessThan(1_000);
  });

  it('encodes and resamples browser audio for the Nemotron stream', () => {
    const buffer = encodePcm16(new Float32Array([-1, -0.5, 0, 0.5, 1]), 20_000, 16_000);
    const pcm = new Int16Array(buffer);

    expect(pcm).toHaveLength(4);
    expect(pcm[0]).toBe(-32_768);
    expect(pcm[2]).toBe(0);
    expect(pcm[3]).toBe(16_383);
  });

  it('encodes browser PCM samples as mono 16-bit WAV', async () => {
    const blob = encodeWav(
      [new Float32Array([-1, -0.5]), new Float32Array([0, 0.5, 1])],
      16_000,
    );
    const view = new DataView(await blob.arrayBuffer());
    const text = (offset: number, length: number) =>
      String.fromCharCode(...Array.from({ length }, (_, index) => view.getUint8(offset + index)));

    expect(blob.type).toBe('audio/wav');
    expect(text(0, 4)).toBe('RIFF');
    expect(text(8, 4)).toBe('WAVE');
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(view.getUint32(40, true)).toBe(10);
    expect(view.getInt16(44, true)).toBe(-32_768);
    expect(view.getInt16(52, true)).toBe(32_767);
  });
});
