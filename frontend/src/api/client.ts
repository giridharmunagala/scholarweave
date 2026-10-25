const API_BASE = import.meta.env.VITE_API_BASE || '/api';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const details = await readError(response);
    const message =
      typeof details === 'object' && details && 'message' in details
        ? String(details.message)
        : typeof details === 'object' && details && 'detail' in details
          ? typeof details.detail === 'string' ? details.detail : JSON.stringify(details.detail)
          : typeof details === 'string' && details
            ? details
            : `${response.status} ${response.statusText}`;
    throw new ApiError(response.status, message, details);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function json(method: string, body?: unknown): RequestInit {
  return {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

async function readError(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text.trim()) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return { message: text };
  }
}
