import { apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';
import type { ModelReference } from '../chat/api';

export type BriefCatalog = components['schemas']['BriefCatalogResponse'];
export type BriefEdition = components['schemas']['BriefEditionResponse'];
export type BriefItem = components['schemas']['BriefItemResponse'];
export type BriefPreferences = components['schemas']['BriefPreferences'];
export type BriefPreview = components['schemas']['BriefPreviewResponse'];
export type BriefHighlight = components['schemas']['BriefHighlightSummary'];

export const loadCatalog = () => request<BriefCatalog>('/brief/catalog');
export const loadBrief = () => request<BriefEdition>('/brief');
export const generateHighlights = (model_reference: ModelReference | null = null) =>
  request<BriefEdition>('/brief/highlights', json('POST', { model_reference }));
export const loadBriefHistory = (offset = 0) =>
  request<BriefHighlight[]>(`/brief/summaries?limit=25&offset=${offset}`);
export const refreshBrief = (force = false) => request<BriefEdition>('/brief/refresh', json('POST', { force }));
export const clearBrief = () => request<BriefEdition>('/brief', json('DELETE'));
export const fetchBriefItem = (id: string) => request<BriefPreview>(`/brief/items/${id}/fetch`, json('POST'));
export const keepBriefItem = (id: string) => request<BriefItem>(`/brief/items/${id}/keep`, json('POST'));
export const discardBriefItem = (id: string) => request<BriefEdition>(`/brief/items/${id}`, json('DELETE'));
export const savePreferences = (preferences: BriefPreferences) =>
  request<BriefCatalog>('/brief/preferences', json('PUT', preferences));
export const previewUrl = (id: string) => apiUrl(`/brief/items/${id}/raw`);
