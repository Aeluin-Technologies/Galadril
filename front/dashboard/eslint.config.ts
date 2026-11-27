import { fileURLToPath } from "node:url";
import withNuxt from "./.nuxt/eslint.config.mjs";
import { typePolicy } from "../testing/type-policy.ts";

export default withNuxt({
  files: ["**/*.{ts,tsx,mts,cts,vue}"],
  languageOptions: {
    parserOptions: {
      project: ["./tsconfig.dashboard.json", "../tsconfig.tools.json"],
      tsconfigRootDir: fileURLToPath(new URL(".", import.meta.url)),
      extraFileExtensions: [".vue"],
    },
  },
  linterOptions: { noInlineConfig: true },
  rules: {
    ...typePolicy,
    // Use the void-element form emitted by Prettier in Vue templates.
    "vue/html-self-closing": ["error", { html: { void: "always" } }],
  },
});
