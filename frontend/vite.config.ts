import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  plugins: [preact()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          markdown: ["marked", "dompurify"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:18790",
      "/auth": "http://127.0.0.1:18790",
    },
  },
});
