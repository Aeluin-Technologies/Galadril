declare module "vue" {
  interface ComponentCustomProperties {
    $t: (key: string) => string;
  }
}

export default defineNuxtPlugin(() => ({
  provide: { t: (key: string) => (key === "actions.close" ? "Close" : key) },
}));
