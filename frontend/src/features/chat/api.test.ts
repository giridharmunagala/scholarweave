// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { chatApi, type ResponseEffort } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('chat attachment requests', () => {
  it('uploads one file as multipart form data without setting a JSON content type', async () => {
    const attachment = {
      path: 'research/papers/paper-1/text.md',
      name: 'paper.pdf',
      media_type: 'application/pdf',
      size_bytes: 123,
      document_id: 'paper-1',
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 201, json: async () => attachment,
    });
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['%PDF'], 'paper.pdf', { type: 'application/pdf' });
    const controller = new AbortController();

    expect(await chatApi.uploadAttachment(file, controller.signal)).toEqual(attachment);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/agent/attachments');
    expect(init.method).toBe('POST');
    expect(init.signal).toBe(controller.signal);
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).getAll('file')).toEqual([file]);
    expect(init.headers).toEqual({ Accept: 'application/json' });
  });

  it('sends attachment paths only when files are attached', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchMock);

    await chatApi.send('conv 1', 'Explain this');
    await chatApi.send('conv 1', 'Explain this', {
      response_effort: 'quick',
      context_window_tokens: 32768,
      attachment_paths: ['research/uploads/notes.md', 'research/papers/paper-1/text.md'],
    });
    await chatApi.send('conv 1', 'Explain this', { attachment_paths: [] });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/agent/conversations/conv%201/messages');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).not.toHaveProperty('attachment_paths');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({
      content: 'Explain this',
      response_effort: 'quick',
      attachment_paths: ['research/uploads/notes.md', 'research/papers/paper-1/text.md'],
    });
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).not.toHaveProperty('attachment_paths');
  });

  it.each([undefined, 'auto', 'quick', 'thorough'] as (ResponseEffort | undefined)[])(
    'sends %s effort without legacy selectors',
    async (effort) => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true, status: 200, json: async () => ({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      await chatApi.send('conv 1', 'Discuss this', {
        response_effort: effort,
        web_enabled: false,
        reasoning_effort: 'high',
        context_window_tokens: 32768,
      });
      expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
        content: 'Discuss this',
        response_effort: effort ?? 'auto',
        web_enabled: false,
        reasoning_effort: 'high',
        context_window_tokens: 32768,
      });
    },
  );

  it('defaults to Auto with web access', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchMock);
    await chatApi.send('conv 1', 'Hello');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      content: 'Hello', response_effort: 'auto', web_enabled: true,
    });
  });

  it('surfaces upload errors without starting a conversation or run', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false, status: 413, json: async () => ({ message: 'File exceeds the upload limit' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(chatApi.uploadAttachment(new File(['notes'], 'notes.txt')))
      .rejects.toThrow('File exceeds the upload limit');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
