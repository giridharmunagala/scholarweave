import { json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Settings = components['schemas']['SettingsResponse'];
export type SettingsUpdate = components['schemas']['SettingsUpdate'];
export type Provider = components['schemas']['ProviderResponse'];
export type ProviderCreate = components['schemas']['ProviderCreate'];
export type ProviderUpdate = components['schemas']['ProviderUpdate'];
export type ProviderModels = components['schemas']['ProviderModelsResponse'];
export type ProviderVerification = components['schemas']['ProviderVerifyResponse'];

export function modelIsEnabled(model: Provider['models'][number]): boolean {
  return model.enabled;
}

export function defaultSmallTaskModel(settings: Pick<Settings, 'default_model_references'>) {
  const reference = settings.default_model_references.small_tasks;
  return reference?.provider_profile_id || reference?.model
    ? reference : settings.default_model_references.chat ?? {};
}

export function defaultSummaryModel(settings: Pick<Settings, 'default_model_references'>) {
  const reference = settings.default_model_references.summary;
  return reference?.provider_profile_id || reference?.model
    ? reference : defaultSmallTaskModel(settings);
}

export function modelSupportsVision(
  providers: Provider[],
  reference: { provider_profile_id?: string | null; model?: string | null },
): boolean {
  const provider = reference.provider_profile_id
    ? providers.find((item) => item.id === reference.provider_profile_id)
    : providers.find((item) => item.name === 'Default Ollama') ?? providers[0];
  const model = provider?.models.find((item) => item.name === reference.model);
  return model?.enabled !== false && Boolean(model?.capabilities?.includes('vision'));
}

export const providersApi = {
  settings: () => request<Settings>('/settings'),
  updateSettings: (payload: SettingsUpdate) =>
    request<Settings>('/settings', json('PUT', payload)),
  list: (includeArchived = false) =>
    request<Provider[]>(`/providers${includeArchived ? '?include_archived=true' : ''}`),
  create: (payload: ProviderCreate) =>
    request<Provider>('/providers', json('POST', payload)),
  update: (id: string, payload: ProviderUpdate) =>
    request<Provider>(`/providers/${encodeURIComponent(id)}`, json('PUT', payload)),
  archive: (id: string) =>
    request<Provider>(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  restore: (id: string) =>
    request<Provider>(`/providers/${encodeURIComponent(id)}/restore`, json('POST')),
  discover: (id: string) =>
    request<ProviderModels>(`/providers/${encodeURIComponent(id)}/models`),
  verify: (id: string, model: string) =>
    request<ProviderVerification>(
      `/providers/${encodeURIComponent(id)}/verify`,
      json('POST', { model }),
    ),
};
