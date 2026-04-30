import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  base: '/',
  server: {
    port: 3000,
    allowedHosts: true,
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
