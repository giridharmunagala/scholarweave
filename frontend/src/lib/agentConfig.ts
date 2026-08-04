/** Pure helpers behind the agent and Python node inspectors, kept here so they are testable. */

/** Models that only produce embeddings can never drive an agent, so they are hidden. */
const EMBEDDING_HINTS = ['embed', 'embedding'];

export function isChatModel(name: string): boolean {
  const lower = name.toLowerCase();
  return !EMBEDDING_HINTS.some((hint) => lower.includes(hint));
}

/** The `{{name}}` references in an agent's instructions, de-duplicated and in order. */
export function instructionPlaceholders(instructions: string): string[] {
  return Array.from(
    new Set(Array.from(instructions.matchAll(/\{\{\s*([\w.]+)\s*\}\}/g)).map((match) => match[1])),
  );
}

export interface SchemaParse {
  value: Record<string, unknown> | null;
  error: string;
}

/** Validates the structured-output schema box: blank clears it, anything else must be a JSON object. */
export function parseOutputSchema(text: string): SchemaParse {
  if (!text.trim()) return { value: null, error: '' };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { value: null, error: error instanceof Error ? error.message : 'That is not valid JSON.' };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { value: null, error: 'A schema must be a JSON object.' };
  }
  return { value: parsed as Record<string, unknown>, error: '' };
}

/** Checks user code declares the entrypoint the node will call, since a missing one fails at run time. */
export function pythonCodeError(code: string, entrypoint: string): string {
  if (!code.trim()) return 'Write a function before running this node.';
  const declared = new RegExp(`def\\s+${entrypoint.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}\\s*\\(`);
  if (!declared.test(code)) {
    return `No \`def ${entrypoint}(inputs)\` found. Rename your function or change the entrypoint below.`;
  }
  return '';
}

/** Turns the comma-separated allowlist box into the list the API expects. */
export function parseImportList(text: string): string[] {
  return text
    .split(',')
    .map((name) => name.trim())
    .filter(Boolean);
}
