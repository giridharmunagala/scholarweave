import { apiWebSocketUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type Settings = components['schemas']['SettingsResponse'];
export type SettingsUpdate = components['schemas']['SettingsUpdate'];
export type Provider = components['schemas']['ProviderResponse'];
export type ProviderCreate = components['schemas']['ProviderCreate'];
export type ProviderUpdate = components['schemas']['ProviderUpdate'];
export type ProviderModels = components['schemas']['ProviderModelsResponse'];
export type ProviderVerification = components['schemas']['ProviderVerifyResponse'];
export type BuiltInSpeechStatus = components['schemas']['BuiltInSpeechStatus'];

export function modelIsEnabled(model: Provider['models'][number]): boolean {
  return model.enabled;
}

export const providersApi = {
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
  transcribe: (file: Blob, reference: { provider_profile_id?: string | null; model?: string | null }) => {
    if (!reference.provider_profile_id || !reference.model) {
      return Promise.reject(new Error('Choose a speech recognition model before recording.'));
    }
    const form = new FormData();
    const extension = file.type.includes('wav')
      ? 'wav'
      : file.type.includes('ogg') ? 'ogg' : 'webm';
    form.append('file', file, `recording.${extension}`);
    form.append('provider_profile_id', reference.provider_profile_id);
    form.append('model', reference.model);
    return request<{ text: string }>('/providers/speech/transcriptions', {
      method: 'POST',
      body: form,
    });
  },
  builtInSpeechStatus: () =>
    request<BuiltInSpeechStatus>('/providers/speech/builtin/status'),
  installBuiltInSpeech: () =>
    request<BuiltInSpeechStatus>('/providers/speech/builtin/install', { method: 'POST' }),
  startBuiltInSpeech: () =>
    request<BuiltInSpeechStatus>('/providers/speech/builtin/start', { method: 'POST' }),
  uninstallBuiltInSpeech: () =>
    request<BuiltInSpeechStatus>('/providers/speech/builtin', { method: 'DELETE' }),
  builtInSpeechSocket: () =>
    new WebSocket(apiWebSocketUrl('/providers/speech/builtin/stream')),
};
