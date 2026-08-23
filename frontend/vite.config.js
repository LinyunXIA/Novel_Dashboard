import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// dev 代理 /api 到 FastAPI（默认 8001，与 CLAUDE.md 启动说明一致）
// 8000 常被本机其他程序占用，按文档约定使用 8001。
// 通过 .env 的 VITE_API_URL 覆盖目标地址。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiUrl = env.VITE_API_URL || 'http://127.0.0.1:8001'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: apiUrl,
          changeOrigin: true,
        },
      },
    },
  }
})