import { json, request } from './client';
import type { components } from './schema.generated';

export async function pickLocalPath(kind: components['schemas']['PathPickerRequest']['kind']) {
  const result = await request<components['schemas']['PathPickerResponse']>('/path-picker', {
    ...json('POST', { kind } satisfies components['schemas']['PathPickerRequest']),
    headers: { 'X-ScholarWeave-Picker': '1' },
  });
  return result.path;
}
