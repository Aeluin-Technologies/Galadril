export default defineNuxtRouteMiddleware(async () => {
  const { administrator, refreshAccount } = useAccount();
  await refreshAccount();
  if (!administrator.value) {
    throw createError({
      statusCode: 403,
      statusMessage: "Administrator access required",
    });
  }
});
