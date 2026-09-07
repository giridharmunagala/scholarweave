import { describe, expect, it } from 'vitest';
import type { components } from '../../api/schema.generated';
import { buildFileTree, notePath } from './WorkspacePage';

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
      file('library/papers/study--paper-b/summary.md'),
      file('library/papers/study--paper-a/notes.md'),
      file('library/papers/study--paper-a/summary.md'),
    ]);

    expect(tree.folders.map((folder) => folder.name)).toEqual(['library']);
    const papers = tree.folders[0].folders[0];
    expect(papers.folders.map((folder) => folder.name)).toEqual([
      'study--paper-a',
      'study--paper-b',
    ]);
    expect(papers.folders[0].files.map((item) => item.name)).toEqual([
      'notes.md',
      'summary.md',
    ]);
  });

  it('uses paper metadata as the document folder display name', () => {
    const summary = file('library/papers/study--paper-a/summary.md');
    summary.paper_id = 'paper-a';
    summary.paper_name = 'Readable paper title';

    const tree = buildFileTree([summary]);

    expect(tree.folders[0].folders[0].folders[0]).toMatchObject({
      name: 'study--paper-a',
      displayName: 'Readable paper title',
    });
  });

  it('keeps reusable notes directly in the knowledge library', () => {
    const note = file('knowledge/kv-cache--52a9d3c1-76a9-4cb0-835e-519382660a1f.md');
    note.note_id = '52a9d3c1-76a9-4cb0-835e-519382660a1f';
    note.note_name = 'KV cache experiments';

    const tree = buildFileTree([note]);

    expect(tree.folders[0].name).toBe('knowledge');
    expect(tree.folders[0].folders).toEqual([]);
    expect(tree.folders[0].files[0].note_name).toBe('KV cache experiments');
  });
});

describe('notePath', () => {
  it('defaults new notes to knowledge without changing explicit relative paths', () => {
    expect(notePath('  Ideas  ')).toBe('knowledge/Ideas.md');
    expect(notePath('Concept.md')).toBe('knowledge/Concept.md');
    expect(notePath('projects/example/notes/Ideas.md')).toBe('projects/example/notes/Ideas.md');
    expect(notePath(' ')).toBe('');
  });
});
