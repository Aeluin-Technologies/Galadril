import ts from "typescript-eslint";
import { typePolicy } from "./type-policy.ts";

export default ts.config(...ts.configs.recommendedTypeChecked, {
  files: ["**/*.ts"],
  languageOptions: { parserOptions: { project: "./tsconfig.json" } },
  linterOptions: { noInlineConfig: true },
  rules: { ...typePolicy },
});
