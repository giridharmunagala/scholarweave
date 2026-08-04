/**
 * Mirrors `backend/ports.py`: which port kinds may connect, and what conversion the
 * value needs on the way. Kept in sync so the canvas never offers a connection the
 * backend would reject, nor rejects one it would happily run.
 */

export type PortCoercion = 'serialise' | 'parse_number' | 'parse_json' | 'widen' | 'narrow';

const COERCIONS: Record<string, PortCoercion> = {
  'json>text': 'serialise',
  'list>text': 'serialise',
  'number>text': 'serialise',
  'text>number': 'parse_number',
  'text>json': 'parse_json',
  'list>json': 'widen',
  'json>list': 'narrow',
};

/**
 * Kinds carrying live Agents SDK objects. They connect only to their own kind — not
 * even to `any` — so a mis-wired agent or tool is an obvious canvas error rather than a
 * confusing failure at run time.
 */
export const OPAQUE_KINDS = new Set(['tool', 'agent', 'guardrail']);

export const COERCION_LABELS: Record<PortCoercion, string> = {
  serialise: 'converted to text',
  parse_number: 'parsed as a number',
  parse_json: 'parsed as JSON',
  widen: 'passed through as JSON',
  narrow: 'read as a list',
};

/** The conversion this pair needs, or `null` when the value passes through untouched. */
export function coercionFor(sourceKind: string, targetKind: string): PortCoercion | null {
  if (sourceKind === targetKind || sourceKind === 'any' || targetKind === 'any') return null;
  return COERCIONS[`${sourceKind}>${targetKind}`] || null;
}

export function kindsCompatible(sourceKind: string, targetKind: string): boolean {
  if (sourceKind === targetKind) return true;
  if (OPAQUE_KINDS.has(sourceKind) || OPAQUE_KINDS.has(targetKind)) return false;
  if (sourceKind === 'any' || targetKind === 'any') return true;
  return `${sourceKind}>${targetKind}` in COERCIONS;
}
