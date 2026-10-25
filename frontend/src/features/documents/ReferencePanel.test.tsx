// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import { request } from '../../api/client';
import { Link, RouterProvider } from '../../app/router';
import { ReferencePanel } from './ReferencePanel';

vi.mock('../../api/client', () => ({
  request: vi.fn(),
  json: (method: string, body: unknown) => ({ method, body: JSON.stringify(body) }),
}));
afterEach(() => { vi.resetAllMocks(); vi.restoreAllMocks(); });

it('edits metadata with immutable keys and revision guarded paper links', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const record = {
    id: 'r1', citation_key: 'Ada1843', title: 'Engine notes', authors: ['Ada Lovelace'], year: '1843',
    doi: '', arxiv_id: '', journal: '', volume: '', number: '', pages: '', publisher: '', url: '', abstract: '',
    document_id: null, related_reference_ids: [], entry_type: 'article',
  };
  vi.mocked(request).mockResolvedValue({ revision: 3, references: [record] });
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => { root.render(<RouterProvider><ReferencePanel papers={[{ id: 'p1', title: 'PDF' }]} /></RouterProvider>); });
  const button = Array.from(element.querySelectorAll('button')).find((item) => item.textContent?.includes('Engine notes'))!;
  await act(async () => { button.click(); });
  const key = Array.from(element.querySelectorAll('input')).find((input) => input.value === 'Ada1843')!;
  expect(key.readOnly).toBe(true);
  expect(element.textContent).toContain('Metadata only (no PDF)');
  await act(async () => { element.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
  const call = vi.mocked(request).mock.calls.at(-1)!;
  expect(call[0]).toBe('/references/r1');
  expect(JSON.parse(call[1]!.body as string)).toEqual({ expected_revision: 3, reference: expect.objectContaining({ citation_key: 'Ada1843', authors: ['Ada Lovelace'] }) });
  await act(async () => root.unmount());
});

it('shows load errors without claiming a save succeeded', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(request).mockRejectedValue(new Error('References unavailable'));
  const element = document.createElement('div');
  const root = createRoot(element);
  await act(async () => { root.render(<RouterProvider><ReferencePanel papers={[]} /></RouterProvider>); });
  expect(element.textContent).toContain('References unavailable');
  expect(element.querySelector('fieldset')?.disabled).toBe(true);
  await act(async () => root.unmount());
});

it('guards draft selection and navigation, and explicitly rebases reloads without losing external fields', async () => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  const record = {
    id: 'r1', citation_key: 'Ada1843', title: 'Engine notes', authors: ['Ada Lovelace'], year: '1843',
    doi: '', arxiv_id: '', journal: 'Original journal', volume: '', number: '', pages: '', publisher: '', url: '', abstract: '',
    document_id: null, related_reference_ids: [], entry_type: 'article',
  };
  let remote = { revision: 3, references: [record, { ...record, id: 'r2', citation_key: 'Second', title: 'Second paper' }] };
  vi.mocked(request).mockImplementation(async () => remote);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const guard = vi.fn();
  const element = document.createElement('div');
  const root = createRoot(element);
  const previousPath = window.location.pathname;
  await act(async () => { root.render(<RouterProvider><ReferencePanel papers={[]} onGuardChange={guard} /><Link to="/blocked">Leave</Link></RouterProvider>); });
  const button = (label: string) => Array.from(element.querySelectorAll('button')).find((item) => item.textContent?.includes(label))!;
  const input = (label: string) => Array.from(element.querySelectorAll('label')).find((item) => item.textContent?.startsWith(label))!.querySelector('input')!;
  await act(async () => { button('Engine notes').click(); });
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input('Reference title'), 'My edited title');
    input('Reference title').dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(guard).toHaveBeenLastCalledWith({ dirty: true, busy: false });
  await act(async () => { button('New reference').click(); button('Second paper').click(); element.querySelector('a')!.click(); });
  expect(input('Reference title').value).toBe('My edited title');
  expect(window.location.pathname).toBe(previousPath);
  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);

  vi.mocked(request).mockRejectedValueOnce(new Error('References changed. Reload before saving; no changes were written.'));
  await act(async () => { element.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
  expect(element.textContent).toContain('Reload before saving');
  expect(input('Reference title').value).toBe('My edited title');

  remote = { revision: 4, references: [{ ...record, title: 'External title', journal: 'External journal' }] };
  await act(async () => { button('Reload references').click(); });
  expect(input('Reference title').value).toBe('My edited title');
  expect(button('Save reference').disabled).toBe(true);
  expect(element.textContent).toContain('References changed — compare before saving');
  await act(async () => { button('Rebase my edited fields').click(); });
  expect(input('Reference title').value).toBe('My edited title');
  expect(input('Journal').value).toBe('External journal');
  expect(button('Save reference').disabled).toBe(false);
  await act(async () => { element.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
  const save = vi.mocked(request).mock.calls.at(-1)!;
  expect(JSON.parse(save[1]!.body as string)).toEqual({ expected_revision: 4, reference: expect.objectContaining({ title: 'My edited title', journal: 'External journal' }) });
  expect(confirm).toHaveBeenCalled();
  await act(async () => root.unmount());
});
