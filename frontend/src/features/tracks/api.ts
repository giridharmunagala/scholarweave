import { ApiError, apiUrl, json, request } from '../../api/client';
import type { components } from '../../api/schema.generated';

export const SECTIONS = ['problem', 'literature', 'experiments', 'results'] as const;
export type TrackSection = components['schemas']['TrackSectionResponse']['section'];
export type SectionContent = components['schemas']['TrackSectionResponse'];
export type Track = components['schemas']['TrackDetailResponse'];
export type TrackRevision = components['schemas']['RevisionResponse'];
export type ImportPreview = components['schemas']['ImportPreviewResponse'];

const base = (id: string) => `/tracks/${encodeURIComponent(id)}`;
const sectionPath = (id: string, section: TrackSection) => section.startsWith('exploration/')
  ? `${base(id)}/exploration/${encodeURIComponent(section.slice('exploration/'.length))}`
  : `${base(id)}/sections/${encodeURIComponent(section)}`;
export const tracksApi = {
  list: () => request<Track[]>('/tracks'),
  create: (title: string) => request<Track>('/tracks', json('POST', { title } satisfies components['schemas']['TrackCreateRequest'])),
  get: (id: string) => request<Track>(base(id)),
  section: (id: string, section: TrackSection) => request<SectionContent>(sectionPath(id, section)),
  save: (id: string, section: TrackSection, content: string, expected_sha256: string | null) =>
    request<SectionContent>(sectionPath(id, section), json('PUT', { content, expected_sha256 } satisfies components['schemas']['TrackSectionWriteRequest'])),
  history: (id: string, section: TrackSection) => request<TrackRevision[]>(`${sectionPath(id, section)}/history`),
  revision: (id: string, section: TrackSection, revision: string) =>
    request<SectionContent>(`${sectionPath(id, section)}/history/${encodeURIComponent(revision)}`),
  paper: (id: string, paperId: string, linked: boolean) =>
    request<Track>(`${base(id)}/papers/${encodeURIComponent(paperId)}`, { method: linked ? 'PUT' : 'DELETE' }),
  conversation: (id: string, conversationId: string, linked: boolean) =>
    request<Track>(`${base(id)}/conversations/${encodeURIComponent(conversationId)}`, { method: linked ? 'PUT' : 'DELETE' }),
  preview: (id: string, section: TrackSection, content: string) =>
    request<ImportPreview>(`${base(id)}/imports/preview`, json('POST', { section, content } satisfies components['schemas']['ImportPreviewRequest'])),
  previewZip: (id: string, file: File) => {
    const body = new FormData();
    body.append('file', file);
    return request<ImportPreview>(`${base(id)}/imports/preview-zip`, { method: 'POST', body });
  },
  applyImport: (id: string, preview: ImportPreview, mode: 'merge' | 'replace', merged_content: Record<string, string> = {}) =>
    request<Track>(`${base(id)}/imports`, json('POST', {
      preview_id: preview.preview_id, mode,
      expected_revisions: Object.fromEntries(preview.sections.map((item) => [item.section, item.expected_sha256])),
      confirmed_replace: mode === 'replace', merged_content,
    } satisfies components['schemas']['ImportApplyRequest'])),
  exportUrl: (id: string, section?: TrackSection) =>
    apiUrl(`${base(id)}/export${section ? `?section=${encodeURIComponent(section)}` : ''}`),
  download: async (id: string, section?: TrackSection) => {
    const response = await fetch(tracksApi.exportUrl(id, section));
    if (!response.ok) throw new ApiError(response.status, `Export failed (${response.status} ${response.statusText}). No download was created.`);
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = section ? `${section.split('/').pop()}.md` : `research-track-${id}.zip`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  },
};

export function trackAssistUrl(track: Pick<Track, 'id' | 'title'>, section: TrackSection, skill: string) {
  const research = `Help me with the ${section} section of research track "${track.title}" (track ID: ${track.id}).${skill ? ` Use the ${skill} skill.` : ''} Read the existing section and linked local evidence first. Discuss proposed changes in chat; do not save files unless I explicitly ask.`;
  return `/chat?${new URLSearchParams({ track: track.id, research })}`;
}
