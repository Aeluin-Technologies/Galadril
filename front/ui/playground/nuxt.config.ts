import { fileURLToPath } from "node:url";

export default defineNuxtConfig({
  compatibilityDate: "2025-07-15",
  ssr: false,
  devtools: { enabled: false },
  telemetry: false,
  // Bazel dependency files are read-only; Nitro must normalize its traced copies.
  nitro: { externals: { chmod: true } },
  modules: [fileURLToPath(new URL("../src/nuxt.ts", import.meta.url))],
  app: {
    head: { htmlAttrs: { lang: "en" }, title: "Galadril · UI workspace" },
  },
});
