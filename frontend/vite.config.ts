import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const proxy = {
  '/api': { target: 'http://127.0.0.1:8010', changeOrigin: true },
  '/ws': { target: 'ws://127.0.0.1:8010', ws: true },
}

export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'element-plus': ['element-plus'],
          'charts': ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy,
  },
  preview: { port: 5173, strictPort: true, proxy },
})
