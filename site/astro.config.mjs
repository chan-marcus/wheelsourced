import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  vite: {
    server: {
      proxy: {
        "/api": {
          target: "http://localhost:8420",
          changeOrigin: true,
        },
      },
    },
  },
});
