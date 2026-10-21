<script setup lang="ts">
import { ref } from "vue";
import {
  ArrowPathIcon,
  Squares2X2Icon,
  CircleStackIcon,
  ShieldCheckIcon,
  CommandLineIcon,
  QuestionMarkCircleIcon,
  PlusIcon,
} from "@heroicons/vue/24/outline";

const current = ref("overview");
const navigation = [
  { id: "overview", label: "Overview", icon: Squares2X2Icon },
  { id: "sources", label: "Data sources", icon: CircleStackIcon },
  { id: "access", label: "Access & people", icon: ShieldCheckIcon },
];
const view = ref<{
  create: () => void;
  synchronize: () => Promise<void>;
} | null>(null);
const notice = ref("");
</script>

<template>
  <div
    class="min-h-screen bg-canvas font-sans text-ui text-foreground antialiased"
  >
    <a
      href="#workspace"
      class="sr-only rounded-control bg-primary px-4 py-3 text-on-primary focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-toast"
      >Skip to content</a
    >
    <header
      class="flex min-h-16 items-center justify-between gap-4 border-b border-border bg-surface px-5 lg:px-8"
    >
      <a
        href="/"
        aria-label="Galadril home"
        class="flex items-center gap-3 rounded-control focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
      >
        <span class="grid size-8 place-items-center rounded-control bg-primary"
          ><Squares2X2Icon class="size-5" aria-hidden="true"
        /></span>
        <span class="text-lg font-semibold tracking-tight"
          >galadril<span class="text-primary-foreground">.</span></span
        >
        <span
          class="hidden border-s border-border ps-4 text-ui text-muted sm:block"
          >Operations workspace</span
        >
      </a>
      <div class="flex items-center gap-3">
        <UiBadge tone="neutral">Demo environment</UiBadge
        ><UiIconButton
          :icon="QuestionMarkCircleIcon"
          label="About this demonstration"
          @click="
            notice =
              'This workspace uses local demonstration data. No production source is modified.'
          "
        />
      </div>
    </header>
    <div
      class="mx-auto flex max-w-screen-2xl flex-col lg:min-h-[calc(100vh-4rem)] lg:flex-row"
    >
      <aside
        class="border-b border-border bg-surface px-4 py-5 lg:w-56 lg:shrink-0 lg:border-b-0 lg:border-e lg:py-8"
      >
        <p
          class="mb-3 hidden px-3 text-ui-meta font-semibold uppercase tracking-widest text-muted lg:block"
        >
          Workspace
        </p>
        <nav
          aria-label="Workspace navigation"
          class="flex gap-1 overflow-x-auto lg:flex-col"
        >
          <UiButton
            v-for="item in navigation"
            :key="item.id"
            variant="ghost"
            class="lg:w-full"
            align="start"
            :aria-current="current === item.id ? 'page' : undefined"
            @click="
              current = item.id;
              notice =
                item.id === 'overview'
                  ? ''
                  : 'This is a component demonstration. The operations overview remains visible.';
            "
          >
            <template #leading
              ><component
                :is="item.icon"
                class="size-4"
                aria-hidden="true" /></template
            >{{ item.label }}
          </UiButton>
        </nav>
        <div class="mt-8 hidden border-t border-border px-3 pt-5 lg:block">
          <p class="text-ui-meta font-semibold text-muted">GALADRIL UI / 01</p>
          <p class="mt-2 text-ui-meta leading-5 text-muted">
            One visual language.<br />A clearer way to work.
          </p>
        </div>
      </aside>
      <main
        id="workspace"
        tabindex="-1"
        class="min-w-0 flex-1 space-y-7 px-5 py-7 focus:outline-none lg:px-9 lg:py-9"
      >
        <UiPageHeader
          title="Operations overview"
          eyebrow="Workspace / Overview"
          description="A clear picture of your data, connections and ongoing activity."
        >
          <template #actions
            ><UiButton
              variant="secondary"
              tooltip="Open the source connection form"
              @click="view?.create()"
              ><template #leading><PlusIcon aria-hidden="true" /></template>Add
              a source</UiButton
            >
            <UiButton
              :action="() => view?.synchronize()"
              tooltip="Refresh all source statuses and check their connections"
              ><template #leading><ArrowPathIcon aria-hidden="true" /></template
              >Synchronize sources</UiButton
            ></template
          >
        </UiPageHeader>
        <UiAlert v-if="notice" title="About this workspace">{{
          notice
        }}</UiAlert>
        <WorkspaceView ref="view" />
        <section
          aria-labelledby="components-title"
          class="border-t border-border pt-6"
        >
          <div class="flex flex-wrap items-center justify-between gap-4">
            <div>
              <h2 id="components-title" class="font-semibold">
                Built from shared components
              </h2>
              <p class="mt-1 text-ui-meta text-muted">
                Accessible controls, semantic colors and useful feedback.
              </p>
            </div>
            <span class="flex items-center gap-2 text-ui-meta text-muted"
              ><CommandLineIcon
                class="size-4"
                aria-hidden="true"
              />@galadril/ui</span
            >
          </div>
        </section>
      </main>
    </div>
  </div>
</template>
