// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { RouterProvider } from '../../app/router';
import { ApiError } from '../../api/client';
import { LinkedRepositoryPanel } from './LinkedRepositoryPanel';
import { linkedRepositoryApi as api } from './linkedRepositoryApi';
import { tracksApi } from './api';
import { pickLocalPath } from '../../api/pathPicker';

vi.mock('../../api/pathPicker', () => ({ pickLocalPath: vi.fn() }));

vi.mock('./linkedRepositoryApi', () => ({ linkedRepositoryApi: {
  list: vi.fn(), link: vi.fn(), unlink: vi.fn(), browse: vi.fn(), preview: vi.fn(),
  edit: vi.fn(), artifact: vi.fn(), refresh: vi.fn(), media: vi.fn(), promote: vi.fn(),
} }));
vi.mock('./api', () => ({ tracksApi: { section: vi.fn() } }));
let element: HTMLDivElement;
let root: Root;
const linked = { id: 'repo', root: 'C:\\code', linked_at: 'today', current_revision: null, artifacts: [] };
const preview = { path: 'results.csv', kind: 'text' as const, content: '<script>bad()</script>',
  sha256: 'a'.repeat(64), size_bytes: 20, editable: true };
beforeEach(() => {
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(api.list).mockResolvedValue([linked]);
  vi.mocked(api.browse).mockResolvedValue({ path: '', truncated: false,
    entries: [{ name: 'results.csv', path: 'results.csv', directory: false, size_bytes: 20 }] });
  vi.mocked(api.preview).mockResolvedValue(preview);
  element = document.createElement('div'); document.body.appendChild(element); root = createRoot(element);
});
afterEach(async () => { await act(async () => root.unmount()); element.remove(); vi.unstubAllGlobals(); });
async function render() {
  await act(async () => root.render(<RouterProvider><LinkedRepositoryPanel trackId="track"
    experiments={[{ id: 'EXP-001', experiment: 'Baseline' }]} /></RouterProvider>));
}
async function click(text: string) {
  const button = Array.from(element.querySelectorAll('button')).find((item) => item.textContent === text);
  expect(button).toBeTruthy();
  await act(async () => button!.click());
}
it('requires explicit authorization and previews text without HTML execution', async () => {
  await render();
  expect((Array.from(element.querySelectorAll('button')).find((item) => item.textContent === 'Connect repository') as HTMLButtonElement).disabled).toBe(true);
  await click('C:\\code'); await click('File: results.csv');
  expect(element.querySelector('td')?.textContent).toBe('<script>bad()</script>');
  expect(element.querySelector('script')).toBeNull();
  expect(api.preview).toHaveBeenCalledWith('track', 'repo', 'results.csv');
});
it('picks a folder without connecting or authorizing it and preserves selection on cancel or error', async () => {
  await render();
  expect(pickLocalPath).not.toHaveBeenCalled();
  const connect = () => Array.from(element.querySelectorAll('button')).find((item) => item.textContent === 'Connect repository')!;
  const path = () => element.querySelector<HTMLInputElement>('input[readonly]')!;
  vi.mocked(pickLocalPath).mockResolvedValue('C:\\selected');
  await click('Browse…');
  expect(pickLocalPath).toHaveBeenCalledWith('folder');
  expect(path().value).toBe('C:\\selected');
  expect(connect().disabled).toBe(true);
  expect(api.link).not.toHaveBeenCalled();
  await act(async () => element.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  expect(connect().disabled).toBe(false);
  vi.mocked(pickLocalPath).mockResolvedValue(null);
  await click('Browse…');
  expect(path().value).toBe('C:\\selected');
  expect(connect().disabled).toBe(false);
  vi.mocked(pickLocalPath).mockRejectedValue(new Error('Desktop unavailable'));
  await click('Browse…');
  expect(path().value).toBe('C:\\selected');
  expect(element.querySelector('[role="alert"]')?.textContent).toBe('Desktop unavailable');
  vi.mocked(pickLocalPath).mockResolvedValue('C:\\other');
  await click('Browse…');
  expect(connect().disabled).toBe(true);
  expect(api.link).not.toHaveBeenCalled();
  await act(async () => element.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  vi.mocked(api.link).mockResolvedValue({ ...linked, root: 'C:\\other' });
  await click('Connect repository');
  expect(api.link).toHaveBeenCalledWith('track', 'C:\\other');
});
it('disables the form while the native picker is open', async () => {
  let finish!: (value: string | null) => void;
  vi.mocked(pickLocalPath).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  await render();
  await click('Browse…');
  expect(element.querySelector('fieldset')?.disabled).toBe(true);
  expect(element.querySelector('[role="status"]')?.textContent).toContain('backend desktop');
  await click('Picking…');
  expect(pickLocalPath).toHaveBeenCalledTimes(1);
  await act(async () => finish(null));
  expect(element.querySelector('fieldset')?.disabled).toBe(false);
});
it('preserves a manual draft when stale-save fails', async () => {
  await render(); await click('C:\\code'); await click('File: results.csv'); await click('Edit text manually');
  const textarea = element.querySelector('textarea')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, 'my draft');
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
  vi.mocked(api.edit).mockRejectedValue(new Error('File changed on disk. Reload and reconcile your draft before saving.'));
  await click('Save manual edit');
  expect(api.edit).toHaveBeenCalledWith('track', 'repo', preview, 'my draft');
  expect(element.querySelector('textarea')?.value).toBe('my draft');
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('File changed');
});
it('compares refreshed file contents without discarding a draft and retries only against an explicitly accepted baseline', async () => {
  vi.stubGlobal('confirm', vi.fn(() => true));
  await render(); await click('C:\\code'); await click('File: results.csv'); await click('Edit text manually');
  const changeDraft = async (value: string) => {
    await act(async () => {
      const textarea = element.querySelector('textarea')!;
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, value);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
  };
  await changeDraft('my draft');
  vi.mocked(api.edit).mockRejectedValue(new ApiError(409, 'File changed on disk.'));
  await click('Save manual edit');
  expect(Array.from(element.querySelectorAll('button')).find((button) => button.textContent === 'Save manual edit')!.disabled).toBe(true);
  const current = { ...preview, content: 'External text', sha256: 'b'.repeat(64) };
  vi.mocked(api.preview).mockResolvedValue(current);
  vi.mocked(api.refresh).mockResolvedValue(linked);
  await click('Refresh files & artifact hashes');
  expect(element.querySelector('textarea')!.value).toBe('my draft');
  expect(element.textContent).toContain('External text');
  expect(element.textContent).toContain('Original edit baseline');
  expect(element.textContent).toContain(preview.content);
  expect(api.edit).toHaveBeenCalledTimes(1);
  await click('Use compared file as save baseline');
  expect(element.textContent).toContain('External text');
  await changeDraft('my draft\nExternal text');
  await click('Save manual edit');
  expect(api.edit).toHaveBeenLastCalledWith('track', 'repo', current, 'my draft\nExternal text');
  expect(element.querySelector('textarea')!.value).toBe('my draft\nExternal text');
  expect(element.textContent).toContain('External text');
  const newer = { ...current, content: 'External text changed again', sha256: 'c'.repeat(64) };
  vi.mocked(api.preview).mockResolvedValue(newer);
  await click('Compare current file');
  await click('Use compared file as save baseline');
  vi.mocked(api.edit).mockResolvedValue({ ...newer, content: 'my draft\nExternal text' });
  await click('Save manual edit');
  expect(api.edit).toHaveBeenLastCalledWith('track', 'repo', newer, 'my draft\nExternal text');
  expect(element.querySelector('textarea')).toBeNull();
});

it('keeps an unreconcilable draft recoverable when the external file is no longer text', async () => {
  await render(); await click('C:\\code'); await click('File: results.csv'); await click('Edit text manually');
  await act(async () => {
    const textarea = element.querySelector('textarea')!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, 'Retain this draft');
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
  vi.mocked(api.preview).mockResolvedValue({ ...preview, kind: 'binary', content: null, editable: false, sha256: 'b'.repeat(64) });
  await click('Compare current file');
  expect(element.textContent).toContain('no longer editable text');
  expect(element.querySelector('textarea')!.value).toBe('Retain this draft');
  const accept = Array.from(element.querySelectorAll('button')).find((button) => button.textContent === 'Use compared file as save baseline')!;
  expect(accept.disabled).toBe(true);
  const createObjectURL = vi.fn(() => 'blob:manual-draft');
  const revokeObjectURL = vi.fn();
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
  const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  await click('Download manual draft');
  expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
  expect(download).toHaveBeenCalledOnce();
  expect(download.mock.contexts[0]).toHaveProperty('download', 'results.csv.draft.txt');
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:manual-draft');
  expect(api.edit).not.toHaveBeenCalled();
  download.mockRestore();
});
it('unlink confirms that files are retained and never requests deletion', async () => {
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.mocked(api.unlink).mockResolvedValue();
  await render(); await click('Unlink C:\\code');
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('will not be deleted'));
  expect(api.unlink).toHaveBeenCalledWith('track', 'repo');
  expect(element.textContent).toContain('No repositories connected');
});

it('reopens an artifact and promotes only after explicit confirmation, retaining stale observations', async () => {
  vi.mocked(api.list).mockResolvedValue([{ ...linked, artifacts: [{
    id: 'receipt', path: 'results.csv', experiment_id: 'EXP-001', sha256: preview.sha256,
    size_bytes: 20, linked_at: 'today', observed_revision: null, producing_revision: null,
    provenance: 'User linked', note: '', status: 'unchanged',
  }] }]);
  vi.mocked(tracksApi.section).mockResolvedValue({
    section: 'results', path: 'results.md', sha256: 'b'.repeat(64), modified_at: 'today', content: '# Existing results',
  });
  await render(); await click('C:\\code'); await click('Open artifact: results.csv');
  expect(api.preview).toHaveBeenCalledWith('track', 'repo', 'results.csv');
  await click('Promote EXP-001 receipt to Results');
  expect(api.promote).not.toHaveBeenCalled();
  const textarea = element.querySelector('textarea')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, 'Needs replication');
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
  vi.mocked(api.promote).mockRejectedValue(new Error('Results changed. Reload the Results baseline.'));
  await click('Append receipt and observation to Results');
  expect(api.promote).toHaveBeenCalledWith('track', 'repo', 'receipt', 'b'.repeat(64), 'Needs replication');
  expect(element.querySelector('textarea')?.value).toBe('Needs replication');
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('Results changed');
});
