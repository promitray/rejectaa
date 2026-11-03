import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// In development the auth routes go through this proxy, exactly like the
// Netlify proxy in production, so the refresh cookie is same-origin.
// The proxy target follows VITE_API_URL from .env (so one setting moves both
// the direct API calls and the auth proxy), unless VITE_DEV_API_TARGET is set.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const apiTarget = env.VITE_DEV_API_TARGET || env.VITE_API_URL || 'http://localhost:8000'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api/auth': {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/api\/auth/, '/auth'),
        },
      },
    },
  }
})
