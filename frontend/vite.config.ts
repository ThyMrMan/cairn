import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Built straight into the package so FastAPI serves it from one place and
  // the Docker image needs no separate static-file step.
  build: {
    outDir: "../backend/cairn/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    // In dev the SPA runs on 5173 and the API on 8080. Proxying keeps
    // everything same-origin so the session cookie behaves exactly as it
    // will in production — cross-origin dev would need CORS that production
    // must never have.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: false,
      },
    },
  },
});
