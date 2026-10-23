<template>
  <main
    class="relative isolate flex min-h-dvh items-center justify-center overflow-hidden bg-canvas px-4 py-10 text-foreground"
  >
    <div
      data-auth-decoration
      aria-hidden="true"
      class="auth-halos pointer-events-none absolute inset-0 -z-10"
    />
    <div class="w-full max-w-sm">
      <div class="mb-6 flex items-center justify-center gap-3">
        <img
          src="/favicon.webp"
          alt=""
          width="28"
          height="28"
          class="size-7 object-contain"
        />
        <span class="text-ui-section font-semibold tracking-tight"
          >Galadril</span
        >
      </div>
      <UiCard aria-labelledby="login-title">
        <div class="p-2 sm:p-3">
          <slot />
        </div>
      </UiCard>
    </div>
  </main>
</template>

<style scoped>
.auth-halos {
  background:
    radial-gradient(
      ellipse 24rem 22rem at 15% 22%,
      rgb(var(--ui-primary) / 0.1),
      transparent 75%
    ),
    radial-gradient(
      ellipse 26rem 24rem at 88% 78%,
      rgb(var(--ui-primary) / 0.07),
      transparent 75%
    );
}
</style>
