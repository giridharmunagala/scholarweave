import { describe, expect, it } from 'vitest';

import type { Provider } from '../providers/api';
import {
  chatModelOptions,
  decodeModelReference,
  encodeModelReference,
  preferredChatModel,
} from './ChatModelPicker';
import { reasoningEffortsForModel } from './ReasoningEffortSelect';

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

  it('uses the last selected chat model ahead of the workspace default', () => {
    const settings = {
      default_model_references: {
        chat: { provider_profile_id: 'provider-1', model: 'default-model' },
      },
      last_chat_model_reference: {
        provider_profile_id: 'provider-2',
        model: 'last-model',
      },
    };

    expect(preferredChatModel(settings)).toEqual({
      provider_profile_id: 'provider-2',
      model: 'last-model',
    });
  });

  it('falls back to the workspace default before a model has been selected', () => {
    const settings = {
      default_model_references: {
        chat: { provider_profile_id: 'provider-1', model: 'default-model' },
      },
      last_chat_model_reference: {},
    };

    expect(preferredChatModel(settings)).toEqual({
      provider_profile_id: 'provider-1',
      model: 'default-model',
    });
  });

  it('uses only the selected model declared reasoning levels', () => {
    const providers = [
      {
        id: 'provider-1',
        models: [
          {
            name: 'qwen',
            enabled: true,
            reasoning_efforts: ['low', 'medium', 'xhigh'],
          },
          { name: 'unknown', enabled: true, reasoning_efforts: null },
        ],
      },
    ] as Provider[];

    expect(
      reasoningEffortsForModel(providers, {
        provider_profile_id: 'provider-1',
        model: 'qwen',
      }),
    ).toEqual(['low', 'medium', 'xhigh']);
    expect(
      reasoningEffortsForModel(providers, {
        provider_profile_id: 'provider-1',
        model: 'unknown',
      }),
    ).toBeNull();
  });
});
