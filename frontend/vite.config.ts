import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnv, type ProxyOptions } from 'vite';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export function backendOrigin(runtimeFile: string): string {
  if (!existsSync(runtimeFile)) return 'http://127.0.0.1:1102';
  const record: unknown = JSON.parse(readFileSync(runtimeFile, 'utf-8'));
  if (typeof record !== 'object' || record === null || !('url' in record)
    || typeof record.url !== 'string' || !/^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::\d+)?\/?$/i.test(record.url)) {
    throw new Error(`Invalid loopback backend address in ${runtimeFile}`);
  }
  return new URL(record.url).origin;
}

export function backendProxy(runtimeFile: string): ProxyOptions {
  return {
    target: backendOrigin(runtimeFile),
    changeOrigin: true,
    configure(_proxy, options) {
      options.bypass = () => {
        options.target = backendOrigin(runtimeFile);
      };
    },
  };
}

export default defineConfig(({ mode }) => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  const env = loadEnv(mode, root, 'SCHOLARWEAVE_');
  const dataDir = process.env.SCHOLARWEAVE_DATA_DIR || env.SCHOLARWEAVE_DATA_DIR || 'local_data';
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      strictPort: false,
      proxy: {
        '/api': backendProxy(resolve(root, dataDir, 'backend-runtime.json')),
      },
    },
    preview: { host: '127.0.0.1', strictPort: false },
    build: {
      outDir: 'dist',
      sourcemap: false,
    },
    test: {
      globals: true,
      environment: 'node',
      setupFiles: ['./src/test/setup.ts'],
      environmentOptions: {
        jsdom: {
          url: 'http://localhost/',
        },
      },
      coverage: {
        provider: 'v8',
        reporter: ['text', 'html'],
      },
    },
  };
});
