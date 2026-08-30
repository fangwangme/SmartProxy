import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // The Flask API from `src/api/server.py`.
      '/api': 'http://127.0.0.1:6942',
    },
  },
  build: {
    // Served by Flask as its `static_folder` (see src/api/server.py).
    outDir: '../.local/dist',
    emptyOutDir: true,
  },
})
