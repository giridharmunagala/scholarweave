import { describe, expect, it } from 'vitest';
import type { components } from '../../api/schema.generated';
import { buildFileTree } from './WorkspacePage';

type WorkspaceFile = components['schemas']['WorkspaceFileResponse'];

function file(path: string): WorkspaceFile {
  const parts = path.split('/');
  return {
    path,
    name: parts[parts.length - 1] ?? path,
    media_type: 'text/markdown',
    size_bytes: 1,
    modified_at: '2026-08-06T00:00:00Z',
    tags: [],
    paper_id: null,
    paper_name: null,
    note_id: null,
    note_name: null,
    kind: 'file',
  };
}

describe('buildFileTree', () => {
  it('groups paper files under their document folders', () => {
    const tree = buildFileTree([
      file('papers/paper-b/summary.md'),
      file('papers/paper-a/notes.md'),
      file('papers/paper-a/summary.md'),
    ]);

    expect(tree.folders.map((folder) => folder.name)).toEqual(['papers']);
    expect(tree.folders[0].folders.map((folder) => folder.name)).toEqual([
      'paper-a',
      'paper-b',
    ]);
    expect(tree.folders[0].folders[0].files.map((item) => item.name)).toEqual([
      'notes.md',
      'summary.md',
    ]);
  });

  it('uses paper metadata as the document folder display name', () => {
    const summary = file('papers/paper-a/summary.md');
    summary.paper_id = 'paper-a';
    summary.paper_name = 'Readable paper title';

    const tree = buildFileTree([summary]);

    expect(tree.folders[0].folders[0]).toMatchObject({
      name: 'paper-a',
      displayName: 'Readable paper title',
    });
  });

  it('uses note metadata as the generic note folder display name', () => {
    const note = file('notes/52a9d3c1-76a9-4cb0-835e-519382660a1f/note.md');
    note.note_id = '52a9d3c1-76a9-4cb0-835e-519382660a1f';
    note.note_name = 'KV cache experiments';

    const tree = buildFileTree([note]);

    expect(tree.folders[0].folders[0]).toMatchObject({
      name: note.note_id,
      displayName: 'KV cache experiments',
    });
  });
});
