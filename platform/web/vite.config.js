import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Air-gap: everything is bundled locally, no CDN (spec §9.4). Dev proxies /api and /dicom-web
// to the loopback-published services so the SPA runs against the real stack.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/dicom-web": "http://127.0.0.1:8042",
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.js",
    restoreMocks: true,
    clearMocks: true,
  },
});
