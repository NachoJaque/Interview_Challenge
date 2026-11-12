import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the browser talks to Vite, which forwards /api to the API.
// In Docker, nginx does the same job (see nginx.conf), so the app always
// calls its own origin and needs no CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
