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
          ? JSON.stringify(details.detail)
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

export function apiWebSocketUrl(path: string): string {
  const url = new URL(`${API_BASE}${path}`, window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

async function readError(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return { message: await response.text() };
  }
}
