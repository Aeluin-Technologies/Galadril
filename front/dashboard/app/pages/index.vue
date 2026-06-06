<script setup lang="ts">
import { useOnboardingStore } from "~/stores/useOnboarding";
import {
  ArrowUpRightIcon,
  CheckCircleIcon,
  LockClosedIcon,
  MagnifyingGlassIcon,
} from "@heroicons/vue/24/outline";

const onboarding = useOnboardingStore();
const isCommandOpen = useState<boolean>("global-command-bar", () => false);
const { t } = useI18n();

onMounted(() => {
  onboarding.hydrate();
});

const workflowSteps = computed(() => [
  {
    id: "pipeline",
    index: "01",
    title: t("onboarding.steps.pipeline.title"),
    description: t("onboarding.steps.pipeline.description"),
    label: t("onboarding.steps.pipeline.label"),
    actionType: "link",
    target: "/pipeline/new",
    completed: onboarding.hasCreatedPipeline,
    active: true,
  },
  {
    id: "ingest",
    index: "02",
    title: t("onboarding.steps.ingest.title"),
    description: t("onboarding.steps.ingest.description"),
    label: t("onboarding.steps.ingest.label"),
    actionType: "search",
    completed: onboarding.hasIngestedData,
    active: onboarding.hasCreatedPipeline,
  },
  {
    id: "dashboard",
    index: "03",
    title: t("onboarding.steps.dashboard.title"),
    description: t("onboarding.steps.dashboard.description"),
    label: t("onboarding.steps.dashboard.label"),
    actionType: "link",
    target: "/builder",
    completed: onboarding.hasBuiltDashboard,
    active: onboarding.hasCreatedPipeline && onboarding.hasIngestedData,
  },
]);

const triggerGlobalSearch = () => {
  isCommandOpen.value = true;
};
</script>

<template>
  <div
    class="min-h-full bg-stone-50 py-20 px-8 flex flex-col items-center justify-center font-sans antialiased"
  >
    <OnboardingTooltip
      :step-index="0"
      :title="$t('onboarding.tooltips.ontological_search.title')"
      class="top-[68px] right-[152px]"
      arrow-class="right-[110px]"
    >
      {{ $t("onboarding.tooltips.ontological_search.body") }}
      <kbd
        class="bg-stone-100 border border-zinc-200 px-1.5 py-0.5 rounded text-[10px] font-mono text-zinc-800 shadow-sm font-bold"
        >{{ $t("navbar.search.shortcut") }}</kbd
      >.
    </OnboardingTooltip>

    <OnboardingTooltip
      :step-index="3"
      :is-last-step="true"
      :title="$t('onboarding.tooltips.pengolo_copilot.title')"
      class="top-[68px] right-[24px]"
      arrow-class="right-[45px]"
    >
      {{ $t("onboarding.tooltips.pengolo_copilot.body") }}
    </OnboardingTooltip>

    <div class="max-w-5xl w-full space-y-16">
      <div class="space-y-4 max-w-xl">
        <h1
          class="text-3xl font-bold tracking-tight text-zinc-950 font-sans sm:text-4xl"
        >
          {{ $t("onboarding.header.title") }}
        </h1>
        <p class="text-sm text-zinc-600 leading-relaxed font-normal">
          {{ $t("onboarding.header.description") }}
        </p>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div
          v-for="step in workflowSteps"
          :key="step.id"
          :class="[
            'bg-white border rounded-xl p-6 flex flex-col justify-between transition-all duration-300',
            step.completed
              ? 'border-emerald-200/80 shadow-[0_4px_20px_rgba(16,185,129,0.02)]'
              : 'border-zinc-200/70 shadow-[0_4px_24px_rgba(0,0,0,0.02)]',
            !step.active
              ? 'opacity-40 bg-stone-50/50'
              : 'hover:border-zinc-300',
          ]"
        >
          <div class="space-y-4">
            <div class="flex items-center justify-between">
              <span
                class="text-[11px] font-mono font-bold text-zinc-400 tracking-wider"
                >{{ step.index }}</span
              >
              <div
                v-if="step.completed"
                class="text-emerald-600 flex items-center space-x-1 text-[10px] font-mono font-bold uppercase tracking-wider"
              >
                <CheckCircleIcon class="w-4 h-4 stroke-[2.5]" />
                <span>{{ $t("onboarding.status.completed") }}</span>
              </div>
              <div v-else-if="!step.active" class="text-zinc-400">
                <LockClosedIcon class="w-3.5 h-3.5" />
              </div>
            </div>

            <div class="space-y-1.5">
              <h2 class="text-sm font-bold text-zinc-900">
                {{ step.title }}
              </h2>
              <p class="text-xs text-zinc-600 leading-relaxed">
                {{ step.description }}
              </p>
            </div>
          </div>

          <div
            class="mt-8 pt-4 border-t border-zinc-100 flex items-center justify-between"
          >
            <template v-if="step.completed">
              <span class="text-[11px] font-medium text-emerald-700">{{
                $t("onboarding.status.active_production")
              }}</span>
            </template>
            <template v-else>
              <UtilsButton
                v-if="step.actionType === 'search'"
                variant="ghost"
                :disabled="!step.active"
                @click="step.active && triggerGlobalSearch()"
                :class="[
                  '!px-0 !py-0 !shadow-none space-x-1.5 text-xs font-semibold transition-colors',
                  step.active
                    ? 'text-amber-600 hover:text-amber-700 hover:bg-transparent'
                    : 'text-zinc-400 pointer-events-none',
                ]"
              >
                <MagnifyingGlassIcon class="w-3.5 h-3.5 stroke-[2.5]" />
                <span>{{ step.label }}</span>
                <kbd
                  class="ml-1 text-[10px] font-mono font-bold px-1.5 py-0.5 bg-stone-50 border border-zinc-200 text-zinc-500 rounded shadow-sm"
                  >{{ $t("navbar.search.shortcut") }}</kbd
                >
              </UtilsButton>

              <NuxtLink
                v-else
                :to="step.active ? step.target : '#'"
                :class="[
                  'inline-flex items-center space-x-1 text-xs font-semibold transition-colors',
                  step.active
                    ? 'text-amber-600 hover:text-amber-700'
                    : 'text-zinc-400 pointer-events-none',
                ]"
              >
                <span>{{ step.label }}</span>
                <ArrowUpRightIcon class="w-3 h-3 stroke-[2.5]" />
              </NuxtLink>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
