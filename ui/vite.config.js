import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    port: 5173,
    // API_PROXY — другой адрес API для разработки (по умолчанию FastAPI на 8080).
    proxy: { '/api': process.env.API_PROXY || 'http://127.0.0.1:8080' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
