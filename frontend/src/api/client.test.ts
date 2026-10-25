import { afterEach, expect, it, vi } from 'vitest';
import { ApiError, request } from './client';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each([
  ['Internal Server Error', 'Internal Server Error', { message: 'Internal Server Error' }],
  ['<html>Upstream unavailable</html>', '<html>Upstream unavailable</html>', { message: '<html>Upstream unavailable</html>' }],
  ['{"message":"Try again"}', 'Try again', { message: 'Try again' }],
  ['{"detail":"arXiv unavailable"}', 'arXiv unavailable', { detail: 'arXiv unavailable' }],
  ['{"detail":[{"msg":"Invalid URL"}]}', '[{"msg":"Invalid URL"}]', { detail: [{ msg: 'Invalid URL' }] }],
  ['"Unavailable"', 'Unavailable', 'Unavailable'],
  ['', '502 Bad Gateway', undefined],
  [' \n ', '502 Bad Gateway', undefined],
])('preserves HTTP errors and consumes their body once: %s', async (body, message, details) => {
  const response = new Response(body, { status: 502, statusText: 'Bad Gateway' });
  const readText = vi.spyOn(response, 'text');
  const readJson = vi.spyOn(response, 'json');
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
  const failure = await request('/research/arxiv').catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(ApiError);
  expect(failure).toMatchObject({ status: 502, message, details });
  expect(readText).toHaveBeenCalledTimes(1);
  expect(readJson).not.toHaveBeenCalled();
});
