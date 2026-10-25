import { apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export type RepositoryArtifact = components['schemas']['RepositoryArtifact'];
export type LinkedRepository = components['schemas']['RepositoryLink'];
export type RepositoryListing = components['schemas']['RepositoryListing'];
export type RepositoryPreview = components['schemas']['RepositoryPreview'];
const base = (track: string) => `/tracks/${encodeURIComponent(track)}/repositories`;
const item = (track: string, link: string) => `${base(track)}/${encodeURIComponent(link)}`;
export const linkedRepositoryApi = {
  list: (track: string) => request<LinkedRepository[]>(base(track)),
  link: (track: string, root: string) => request<LinkedRepository>(base(track), json('POST', {
    root, authorize_root: true,
  } satisfies components['schemas']['RepositoryLinkRequest'])),
  unlink: (track: string, link: string) => request<void>(item(track, link), json('DELETE')),
  browse: (track: string, link: string, path = '') =>
    request<RepositoryListing>(`${item(track, link)}/browse?path=${encodeURIComponent(path)}`),
  preview: (track: string, link: string, path: string) =>
    request<RepositoryPreview>(`${item(track, link)}/preview?path=${encodeURIComponent(path)}`),
  media: (track: string, link: string, path: string, hash?: string) =>
    apiUrl(`${item(track, link)}/media?path=${encodeURIComponent(path)}${hash ? `&expected_sha256=${encodeURIComponent(hash)}` : ''}`),
  edit: (track: string, link: string, preview: RepositoryPreview, content: string) =>
    request<RepositoryPreview>(`${item(track, link)}/file`, json('PUT', {
      path: preview.path, content, expected_sha256: preview.sha256,
    } satisfies components['schemas']['RepositoryEditRequest'])),
  artifact: (track: string, link: string, preview: RepositoryPreview, experiment: string, revision: string, note: string) =>
    request<LinkedRepository>(`${item(track, link)}/artifacts`, json('POST', {
      path: preview.path, expected_sha256: preview.sha256, experiment_id: experiment,
      producing_revision: revision || null, note,
    } satisfies components['schemas']['ArtifactLinkRequest'])),
  refresh: (track: string, link: string) =>
    request<LinkedRepository>(`${item(track, link)}/refresh`, json('POST')),
  promote: (track: string, link: string, artifact: string, expectedResultsHash: string, observation: string) =>
    request<components['schemas']['TrackSectionResponse']>(
      `${item(track, link)}/artifacts/${encodeURIComponent(artifact)}/promote`,
      json('POST', { expected_results_sha256: expectedResultsHash, observation } satisfies components['schemas']['ArtifactPromotionRequest']),
    ),
};
