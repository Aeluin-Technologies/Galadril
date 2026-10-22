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

/** Materializes test sources so Vite and Nuxt resolve the same generated tsconfig. */
export function stageFrontend(sourceRoot, application) {
  const root = mkdtempSync(
    join(process.env.TEST_TMPDIR ?? tmpdir(), "galadril-front-"),
  );
  const excluded = new Set(["node_modules", ".nuxt", ".output", ".git"]);
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
  if (existsSync(hostModules)) symlinkSync(hostModules, join(root, "node_modules"), "dir");
  return {
    path: join(root, application),
    cleanup: () => rmSync(root, { recursive: true, force: true }),
  };
}
