export default defineNuxtRouteMiddleware(async (to) => {
  if (to.path === "/login") return;
  try {
    await $fetch("/auth/session", { credentials: "same-origin" });
  } catch {
    return navigateTo("/login");
  }
});
