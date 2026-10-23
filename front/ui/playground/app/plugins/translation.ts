export default defineNuxtPlugin(() => ({
  provide: { t: (key: string) => (key === "actions.close" ? "Close" : key) },
}));
