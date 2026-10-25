import { afterEach, expect, it, vi } from 'vitest';
import { pickLocalPath } from './pathPicker';

afterEach(() => vi.unstubAllGlobals());

it.each(['file', 'folder'] as const)('requests a %s picker with the protected JSON API', async (kind) => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ path: 'C:\\selected' })));
  vi.stubGlobal('fetch', fetch);
  expect(await pickLocalPath(kind)).toBe('C:\\selected');
  expect(fetch).toHaveBeenCalledWith('/api/path-picker', expect.objectContaining({
    method: 'POST',
    body: JSON.stringify({ kind }),
    headers: expect.objectContaining({
      'Content-Type': 'application/json', 'X-ScholarWeave-Picker': '1',
    }),
  }));
});

it('preserves cancellation and surfaces desktop errors', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ path: null })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ message: 'Desktop unavailable' }), { status: 400 }));
  vi.stubGlobal('fetch', fetch);
  expect(await pickLocalPath('folder')).toBeNull();
  await expect(pickLocalPath('folder')).rejects.toThrow('Desktop unavailable');
});
