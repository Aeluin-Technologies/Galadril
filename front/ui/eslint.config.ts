import { fileURLToPath } from "node:url";
import vue from "eslint-plugin-vue";
import ts from "typescript-eslint";
import { typePolicy } from "../testing/type-policy.ts";

export default ts.config(
  ...ts.configs.recommendedTypeChecked,
  ...vue.configs["flat/essential"],
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: ts.parser } },
  },
  {
    files: ["**/*.{ts,tsx,mts,cts,vue}"],
    languageOptions: {
      parserOptions: {
        project: "./tsconfig.json",
        tsconfigRootDir: fileURLToPath(new URL(".", import.meta.url)),
        extraFileExtensions: [".vue"],
      },
    },
    linterOptions: { noInlineConfig: true },
    rules: {
      ...typePolicy,
      "vue/multi-word-component-names": "off",
      "vue/max-attributes-per-line": "off",
      "vue/html-self-closing": "off",
      "vue/html-indent": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/first-attribute-linebreak": "off",
      "vue/html-closing-bracket-newline": "off",
    },
  },
);
