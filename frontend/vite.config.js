import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev 代理 /api 到 FastAPI（默认 8000）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})