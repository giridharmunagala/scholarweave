import { describe, expect, it } from 'vitest';

import type { Provider } from './api';
import { capabilityOptions } from './ModelDefaultsPanel';

describe('speech model options', () => {
  it('only offers models explicitly configured for speech recognition', () => {
    const providers = [
      {
        id: 'local-studio',
        name: 'Local Studio',
        kind: 'openai_compatible',
        models: [
          { name: 'nemotron-speech-en', capabilities: ['speech'], enabled: true },
          { name: 'chat-model', capabilities: ['chat'], enabled: true },
          { name: 'unknown-model', capabilities: [], enabled: true },
        ],
      },
    ] as Provider[];

    expect(capabilityOptions(providers, 'speech').map((option) => option.model)).toEqual([
      'nemotron-speech-en',
    ]);
  });
});
