export function prettyJson(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export function parseJson(text: string): unknown {
  return JSON.parse(text);
}

export function parseJsonObject(text: string): Record<string, unknown> {
  const value = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Expected a JSON object');
  }
  return value as Record<string, unknown>;
}
