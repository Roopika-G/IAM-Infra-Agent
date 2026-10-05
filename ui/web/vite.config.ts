import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The FastAPI backend (ui/app.py) listens on :8000; proxy /api to it in dev.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://localhost:8000' } },
})
