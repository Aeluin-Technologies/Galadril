import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { basename, join } from "node:path";
import { tmpdir } from "node:os";

/**
 * Isolate generated Nuxt state so Vite and the type checker resolve the same workspace.
 *
 * @param application - Selects the consumer while preserving its shared UI sibling.
 * @returns The staged consumer path and an idempotent cleanup callback.
 */
export function stageFrontend(
  sourceRoot: string,
  application: "ui" | "dashboard",
) {
  const root = mkdtempSync(
    join(process.env.TEST_TMPDIR ?? tmpdir(), "galadril-front-"),
  );
  const excluded = new Set(["node_modules", ".nuxt", ".output", ".git"]);
  for (const name of ["testing", "tsconfig.tools.json"]) {
    const source = join(sourceRoot, name);
    if (existsSync(source))
      cpSync(source, join(root, name), { recursive: true, dereference: true });
  }
  for (const name of ["ui", "dashboard"]) {
    const source = join(sourceRoot, name);
    if (!existsSync(source)) continue;
    const target = join(root, name);
    mkdirSync(target, { recursive: true });
    cpSync(source, target, {
      recursive: true,
      dereference: true,
      filter: (path) => !excluded.has(basename(path)),
    });
    const dependencies = join(source, "node_modules");
    if (existsSync(dependencies)) {
      const modules = join(target, "node_modules");
      mkdirSync(modules);
      for (const dependency of readdirSync(dependencies)) {
        if (dependency === "@galadril") {
          mkdirSync(join(modules, dependency));
          symlinkSync(join(root, "ui"), join(modules, dependency, "ui"), "dir");
        } else
          symlinkSync(
            join(dependencies, dependency),
            join(modules, dependency),
            "dir",
          );
      }
    }
  }
  // Nuxt's development transforms inject host runtime imports into workspace components.
  const hostModules = join(root, "dashboard", "node_modules");
  if (existsSync(hostModules))
    symlinkSync(hostModules, join(root, "node_modules"), "dir");
  return {
    path: join(root, application),
    cleanup: () => rmSync(root, { recursive: true, force: true }),
  };
}
