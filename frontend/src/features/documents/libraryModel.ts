import type { components } from '../../api/schema.generated';

type Paper = components['schemas']['DocumentSummaryResponse'];
type Folder = components['schemas']['PaperFolderResponse'];

export function paperFolderId(paper: Paper): string | null {
  const value = paper.metadata.folder_id;
  return typeof value === 'string' ? value : null;
}

export function sortPapers(papers: Paper[], sort: string): Paper[] {
  return [...papers].sort((a, b) => sort === 'title'
    ? a.title.localeCompare(b.title)
    : sort === 'oldest' ? a.created_at.localeCompare(b.created_at) : b.created_at.localeCompare(a.created_at));
}

export function buildLibraryIndex(papers: Paper[], folders: Folder[]) {
  const byId = new Map(papers.map((paper) => [paper.id, paper]));
  const byFolder = new Map<string, Paper[]>(folders.map((folder) => [folder.id, []]));
  const unfiled: Paper[] = [];
  for (const paper of byId.values()) {
    const id = paperFolderId(paper);
    if (id === null) unfiled.push(paper);
    else {
      const members = byFolder.get(id) ?? [];
      members.push(paper);
      byFolder.set(id, members);
    }
  }
  return { byId, byFolder, unfiled };
}

export function collectionTree(folders: Folder[]) {
  return [...folders].sort((a, b) => {
    const left = a.name.toLowerCase().split('/');
    const right = b.name.toLowerCase().split('/');
    for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
      const comparison = left[i].localeCompare(right[i]);
      if (comparison) return comparison;
    }
    return left.length - right.length;
  }).map((folder) => ({
    ...folder, depth: folder.name.split('/').length - 1, label: folder.name.split('/').at(-1)!,
  }));
}
