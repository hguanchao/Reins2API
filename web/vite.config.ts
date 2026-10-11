import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { createLogger, defineConfig } from 'vite'

/** 静音 Vite 启动 banner（ready / Local / press h 等 info 输出），保留 warn/error */
const logger = createLogger()
logger.info = () => {}

const BACKEND = 'http://127.0.0.1:8787'
const proxyTo = (route: string) => ({
  target: BACKEND,
  changeOrigin: true,
})

/** 开发态将 /api /v1 /healthz 代理到后端（默认 8787） */
export default defineConfig({
  customLogger: logger,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5274,
    strictPort: true,
    proxy: {
      '/api': proxyTo('/api'),
      '/v1': proxyTo('/v1'),
      '/healthz': proxyTo('/healthz'),
    },
  }
})
