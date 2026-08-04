import type { IconName } from '../components/common/Icon';
import type { NodeDefinitionResponse } from '../types/api';

export interface CategoryMeta {
  id: string;
  label: string;
  icon: IconName;
  blurb: string;
  order: number;
}

const CATEGORY_META: Record<string, CategoryMeta> = {
  inputs: { id: 'inputs', label: 'Inputs', icon: 'login', blurb: 'Values and context that enter the agent', order: 0 },
  agents: { id: 'agents', label: 'Agents', icon: 'sparkle', blurb: 'Models that decide, call tools and hand off', order: 1 },
  code: { id: 'code', label: 'Code', icon: 'braces', blurb: 'Your own Python, run in a sandbox', order: 2 },
  custom: { id: 'custom', label: 'Custom', icon: 'braces', blurb: 'Reusable revision-pinned Python transforms', order: 3 },
  documents: { id: 'documents', label: 'Documents', icon: 'papers', blurb: 'Ingest, split and load papers', order: 4 },
  retrieval: { id: 'retrieval', label: 'Retrieval', icon: 'search', blurb: 'Index and pull grounded context', order: 5 },
  llm: { id: 'llm', label: 'LLM', icon: 'cpu', blurb: 'Prompting, generation and embeddings', order: 6 },
  parsing: { id: 'parsing', label: 'Parsing', icon: 'braces', blurb: 'Shape values into text or JSON', order: 7 },
  control: { id: 'control', label: 'Control flow', icon: 'layers', blurb: 'Map, reduce, repeat and budget', order: 8 },
  workflows: { id: 'workflows', label: 'Agents', icon: 'workflow', blurb: 'Your saved agents, reused as tools', order: 9 },
  files: { id: 'files', label: 'Files', icon: 'folder', blurb: 'Read and write workspace files', order: 10 },
  outputs: { id: 'outputs', label: 'Outputs', icon: 'send', blurb: 'Final results and citations', order: 11 },
};

const UNKNOWN_CATEGORY: CategoryMeta = {
  id: 'unknown',
  label: 'Other',
  icon: 'sparkle',
  blurb: 'Uncategorised nodes',
  order: 99,
};

export function categoryMeta(category: string): CategoryMeta {
  return CATEGORY_META[category] ?? { ...UNKNOWN_CATEGORY, id: category || 'unknown', label: category || 'Other' };
}

/** Maps a category onto the `--cat-*` CSS custom properties defined in styles.css. */
export function categoryVars(category: string): Record<string, string> {
  const id = CATEGORY_META[category] ? category : 'unknown';
  const colorId = id === 'custom' ? 'code' : id;
  return {
    '--cat-color': `var(--cat-${colorId})`,
    '--cat-soft': `var(--cat-${colorId}-soft)`,
  };
}

export interface CategoryGroup {
  meta: CategoryMeta;
  nodes: NodeDefinitionResponse[];
}

export function groupByCategory(nodes: NodeDefinitionResponse[]): CategoryGroup[] {
  const buckets = new Map<string, NodeDefinitionResponse[]>();
  for (const node of nodes) {
    const bucket = buckets.get(node.category);
    if (bucket) bucket.push(node);
    else buckets.set(node.category, [node]);
  }
  return [...buckets.entries()]
    .map(([category, categoryNodes]) => ({
      meta: categoryMeta(category),
      nodes: [...categoryNodes].sort((left, right) => left.label.localeCompare(right.label)),
    }))
    .sort((left, right) => left.meta.order - right.meta.order || left.meta.label.localeCompare(right.meta.label));
}

function haystack(node: NodeDefinitionResponse): string {
  return [node.label, node.type, node.category, node.description, ...node.tags].join(' ').toLowerCase();
}

/**
 * Subsequence match so "ollgen" finds "Ollama Generate". Returns a score where
 * lower is better, or null when the term does not match at all.
 */
export function fuzzyScore(term: string, node: NodeDefinitionResponse): number | null {
  const needle = term.trim().toLowerCase();
  if (!needle) return 0;

  const label = node.label.toLowerCase();
  if (label.startsWith(needle)) return 0;
  if (node.type.toLowerCase().startsWith(needle)) return 1;
  if (label.includes(needle)) return 2;

  const text = haystack(node);
  if (text.includes(needle)) return 3;

  let cursor = 0;
  let gaps = 0;
  for (const char of needle) {
    const found = text.indexOf(char, cursor);
    if (found === -1) return null;
    gaps += found - cursor;
    cursor = found + 1;
  }
  return 4 + gaps / 1000;
}

export function searchNodes(nodes: NodeDefinitionResponse[], term: string): NodeDefinitionResponse[] {
  if (!term.trim()) return nodes;
  return nodes
    .map((node) => ({ node, score: fuzzyScore(term, node) }))
    .filter((entry): entry is { node: NodeDefinitionResponse; score: number } => entry.score !== null)
    .sort((left, right) => left.score - right.score || left.node.label.localeCompare(right.node.label))
    .map((entry) => entry.node);
}

/** Splits `text` around the first case-insensitive occurrence of `term`, for <mark> highlighting. */
export function highlightParts(text: string, term: string): [string, string, string] {
  const needle = term.trim().toLowerCase();
  if (!needle) return [text, '', ''];
  const index = text.toLowerCase().indexOf(needle);
  if (index === -1) return [text, '', ''];
  return [text.slice(0, index), text.slice(index, index + needle.length), text.slice(index + needle.length)];
}
