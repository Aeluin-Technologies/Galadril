<template>
  <div class="relative group">
    <slot name="trigger" />
    <div
      class="absolute right-0 mt-2 w-48 bg-white border border-zinc-200 rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 overflow-hidden"
    >
      <slot />
    </div>
  </div>
</template>
