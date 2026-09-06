import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Settings = components['schemas']['SettingsResponse'];
export type SettingsUpdate = components['schemas']['SettingsUpdate'];
export type Provider = components['schemas']['ProviderResponse'];
export type ProviderCreate = components['schemas']['ProviderCreate'];
export type ProviderUpdate = components['schemas']['ProviderUpdate'];
export type ProviderModels = components['schemas']['ProviderModelsResponse'];
export type ProviderVerification = components['schemas']['ProviderVerifyResponse'];
export type ResidencyStatus = components['schemas']['ResidencyResponse'];

export function modelIsEnabled(model: Provider['models'][number]): boolean {
  return model.enabled;
}

export const providersApi = {
  residency: () => request<ResidencyStatus>('/providers/inference/residency'),
  configureResidency: (id: string, enabled: boolean) =>
    request<ResidencyStatus>(`/providers/${encodeURIComponent(id)}/residency`, json('PUT', { enabled })),
  drainResidency: (id: string) =>
    request<ResidencyStatus>(`/providers/${encodeURIComponent(id)}/residency/drain`, json('POST', {})),
  confirmResidency: (id: string, model: string, sessionMode: 'interactive' | 'batch') =>
    request<ResidencyStatus>(`/providers/${encodeURIComponent(id)}/residency/confirm`,
      json('POST', { model, externally_loaded: true, session_mode: sessionMode })),
  settings: () => request<Settings>('/settings'),
  updateSettings: (payload: SettingsUpdate) =>
    request<Settings>('/settings', json('PUT', payload)),
  list: () => request<Provider[]>('/providers'),
  create: (payload: ProviderCreate) =>
    request<Provider>('/providers', json('POST', payload)),
  update: (id: string, payload: ProviderUpdate) =>
    request<Provider>(`/providers/${encodeURIComponent(id)}`, json('PUT', payload)),
  archive: (id: string) =>
    request<Provider>(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  discover: (id: string) =>
    request<ProviderModels>(`/providers/${encodeURIComponent(id)}/models`),
  verify: (id: string, model: string) =>
    request<ProviderVerification>(
      `/providers/${encodeURIComponent(id)}/verify`,
      json('POST', { model }),
    ),
};
