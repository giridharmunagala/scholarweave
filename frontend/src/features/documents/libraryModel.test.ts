import { expect, it } from 'vitest';
import type { components } from '../../api/schema.generated';
import { buildLibraryIndex, collectionTree, sortPapers } from './libraryModel';

type Paper = components['schemas']['DocumentSummaryResponse'];
type Folder = components['schemas']['PaperFolderResponse'];
const paper = (id: string, metadata: Paper['metadata'] = {}): Paper => ({
  id, title: id, metadata, source_filename: `${id}.pdf`, content_type: 'application/pdf',
  status: 'ready', usefulness: 'unrated', page_count: 1, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
});
const folder: Folder = {
  id: 'methods', name: 'Methods', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
};

it('orders nested virtual collections with stable identities and labels', () => {
  const tree = collectionTree([
    { ...folder, id: 'child', name: 'Methods/Study' }, folder,
    { ...folder, id: 'grandchild', name: 'Methods/Study/Details' },
    { ...folder, id: 'separate', name: 'Methods A' },
  ]);
  expect(tree.map(({ id, depth, label }) => ({ id, depth, label }))).toEqual([
    { id: 'methods', depth: 0, label: 'Methods' },
    { id: 'child', depth: 1, label: 'Study' },
    { id: 'grandchild', depth: 2, label: 'Details' },
    { id: 'separate', depth: 0, label: 'Methods A' },
  ]);
});

it('indexes canonical papers independently from their folder memberships without changing metadata', () => {
  const filed = paper('filed', { folder_id: folder.id, doi: 'example' });
  const unfiled = paper('unfiled');
  const index = buildLibraryIndex([filed, unfiled], [folder]);
  expect(index.byId.get('filed')).toBe(filed);
  expect(index.byFolder.get(folder.id)).toEqual([filed]);
  expect(index.unfiled).toEqual([unfiled]);
  expect(filed.metadata).toEqual({ folder_id: folder.id, doi: 'example' });
});

it('rebuilds membership after moves and folder deletion without duplicating papers', () => {
  const index = buildLibraryIndex([paper('moved'), paper('kept', { folder_id: folder.id })], [folder]);
  expect(index.unfiled.map((item) => item.id)).toEqual(['moved']);
  expect(index.byFolder.get(folder.id)?.map((item) => item.id)).toEqual(['kept']);
  const deleted = buildLibraryIndex([paper('moved'), paper('kept')], []);
  expect(deleted.byId.size).toBe(2);
  expect(deleted.unfiled).toHaveLength(2);
});

it('sorts by acquisition instant including time and timezone offsets without mutating the library', () => {
  const papers = [
    { ...paper('early'), created_at: '2026-01-02T08:00:00+05:30' },
    { ...paper('late'), created_at: '2026-01-02T03:00:00Z' },
    { ...paper('first'), created_at: '2026-01-01T23:00:00Z' },
  ];
  expect(sortPapers(papers, 'newest').map((item) => item.id)).toEqual(['late', 'early', 'first']);
  expect(sortPapers(papers, 'oldest').map((item) => item.id)).toEqual(['first', 'early', 'late']);
  expect(sortPapers(papers, 'title').map((item) => item.id)).toEqual(['early', 'first', 'late']);
  expect(papers.map((item) => item.id)).toEqual(['early', 'late', 'first']);
});
