import {
  addComponentsDir,
  createResolver,
  defineNuxtModule,
  installModule,
} from "@nuxt/kit";
import type {} from "@nuxtjs/tailwindcss";
import type { Nuxt } from "@nuxt/schema";
import type { Config } from "tailwindcss";
import preset from "./tailwind";

export default defineNuxtModule({
  meta: {
    name: "@galadril/ui",
    configKey: "galadrilUi",
    compatibility: { nuxt: "^4.0.0" },
  },
  async setup(_options: Record<string, never>, nuxt: Nuxt) {
    const { resolve } = createResolver(import.meta.url);
    const tokens = resolve("./tokens.css");
    if (!nuxt.options.css.includes(tokens)) nuxt.options.css.unshift(tokens);
    nuxt.options.build.transpile.push("@galadril/ui");
    for (const directory of ["components", "patterns"]) {
      addComponentsDir({
        path: resolve(`./${directory}`),
        prefix: "Ui",
        pathPrefix: false,
        extensions: ["vue"],
      });
    }
    nuxt.hook("tailwindcss:config", (config: Partial<Config>) => {
      config.presets = [...(config.presets ?? []), preset];
      const files = Array.isArray(config.content)
        ? config.content
        : (config.content?.files ?? []);
      files.push(resolve("./**/*.vue"), resolve("./**/*.ts"));
      if (!config.content) config.content = files;
    });
    await installModule("@nuxtjs/tailwindcss");
    nuxt.options.postcss.plugins["tailwindcss/nesting"] = false;
  },
});
