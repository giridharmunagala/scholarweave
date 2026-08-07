import { describe, expect, it } from 'vitest';

import type { Provider } from '../providers/api';
import {
  chatModelOptions,
  decodeModelReference,
  encodeModelReference,
} from './ChatModelPicker';

describe('builder chat model picker', () => {
  it('offers tool-capable and undeclared models but not embedding-only models', () => {
    const providers = [
      {
        id: 'provider-1',
        name: 'Local models',
        models: [
          { name: 'builder', capabilities: ['chat', 'tools'], enabled: true },
          { name: 'unknown', capabilities: [], enabled: true },
          { name: 'disabled', capabilities: ['chat', 'tools'], enabled: false },
          { name: 'embed', capabilities: ['embedding'], enabled: true },
        ],
      },
    ] as Provider[];

    expect(chatModelOptions(providers).map((option) => option.label)).toEqual([
      'Local models / builder',
      'Local models / unknown',
    ]);
  });

  it('round trips a model reference', () => {
    const reference = { provider_profile_id: 'provider-1', model: 'builder' };

    expect(decodeModelReference(encodeModelReference(reference))).toEqual(reference);
    expect(decodeModelReference('')).toEqual({});
  });
});
