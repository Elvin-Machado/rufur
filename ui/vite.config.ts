import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The daemon serves the build from src/rufur/static; `npm run dev` proxies API calls to it.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/rufur/static", emptyOutDir: true },
  server: { proxy: { "/v1": process.env.RUFUR_URL ?? "http://127.0.0.1:7437" } },
});
