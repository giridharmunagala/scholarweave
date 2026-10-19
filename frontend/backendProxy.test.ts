import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer as createHttpServer, type Server } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'vite';
import { afterEach, expect, it } from 'vitest';
import { backendOrigin, backendProxy } from './vite.config.ts';

const directories: string[] = [];
const servers: Server[] = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  })));
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true });
});

function runtimeFile() {
  const directory = mkdtempSync(join(tmpdir(), 'scholarweave-proxy-'));
  directories.push(directory);
  return join(directory, 'backend-runtime.json');
}

async function backend(label: string) {
  const server = createHttpServer((_request, response) => {
    response.setHeader('content-type', 'text/event-stream');
    response.end(`event: status\ndata: ${label}\n\n`);
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Expected a TCP address');
  return { url: `http://127.0.0.1:${address.port}`, port: address.port };
}

it('uses the legacy port only when no launcher discovery file exists', () => {
  expect(backendOrigin(runtimeFile())).toBe('http://127.0.0.1:1102');
});

it.each(['https://example.com', 'http://example.com', 'http://127.0.0.1:8000/api', 'http://user@localhost:8000'])(
  'rejects a non-loopback origin rather than forwarding requests to %s', (url) => {
    const file = runtimeFile();
    writeFileSync(file, JSON.stringify({ url }));
    expect(() => backendOrigin(file)).toThrow('Invalid loopback backend address');
  },
);

it('surfaces malformed discovery metadata', () => {
  const file = runtimeFile();
  writeFileSync(file, '{broken');
  expect(() => backendOrigin(file)).toThrow();
});

it('changes the frontend port when busy and follows backend port changes without restarting', async () => {
  const first = await backend('first-backend');
  const second = await backend('second-backend');
  const file = runtimeFile();
  writeFileSync(file, JSON.stringify({ url: first.url }));
  const vite = await createServer({
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { host: '127.0.0.1', port: first.port, strictPort: false, proxy: { '/api': backendProxy(file) } },
  });
  try {
    await vite.listen();
    const address = vite.httpServer?.address();
    if (!address || typeof address === 'string') throw new Error('Expected a frontend TCP address');
    expect(address.port).not.toBe(first.port);
    const url = `http://127.0.0.1:${address.port}/api/events`;
    expect(await (await fetch(url)).text()).toContain('data: first-backend');
    writeFileSync(file, JSON.stringify({ url: second.url }));
    expect(await (await fetch(url)).text()).toContain('data: second-backend');
  } finally {
    await vite.close();
  }
});
