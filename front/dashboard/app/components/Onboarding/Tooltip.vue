<script setup lang="ts">
import { SparklesIcon, ArrowRightIcon } from "@heroicons/vue/24/outline";
import { useOnboardingStore } from "~/stores/useOnboarding";
import { useEventListener, useResizeObserver } from "@vueuse/core";

const props = defineProps<{
  stepIndex: number;
  isLastStep?: boolean;
  title: string;
  arrowClass?: string;
  target?: string;
}>();

const onboardingStore = useOnboardingStore();
const pageActive = ref(true);
onActivated(() => {
  pageActive.value = true;
  nextTick(updatePosition);
});
onDeactivated(() => {
  pageActive.value = false;
});
const anchor = ref<HTMLElement | SVGElement | null>(null);
const position = ref<Record<string, string>>();
const arrowPosition = ref<Record<string, string>>();

function updatePosition() {
  if (props.target)
    anchor.value = document.querySelector<HTMLElement | SVGElement>(
      props.target,
    );
  const bounds = anchor.value?.getBoundingClientRect();
  if (!bounds) return;
  const width = Math.min(340, window.innerWidth - 24);
  const center = bounds.left + bounds.width / 2;
  const left = Math.max(
    12,
    Math.min(center - width / 2, window.innerWidth - width - 12),
  );
  position.value = {
    top: `${bounds.bottom + 14}px`,
    left: `${left}px`,
    right: "auto",
    width: `${width}px`,
  };
  arrowPosition.value = {
    left: `${Math.max(16, Math.min(center - left - 6, width - 28))}px`,
    right: "auto",
  };
}
useEventListener("resize", updatePosition);
useResizeObserver(anchor, updatePosition);

onMounted(() => {
  onboardingStore.hydrate();
  if (props.target) {
    anchor.value = document.querySelector<HTMLElement | SVGElement>(
      props.target,
    );
    updatePosition();
  }
});

const shouldShow = computed(() => {
  return (
    pageActive.value &&
    !onboardingStore.isTutorialCompleted &&
    onboardingStore.currentTutorialStepIndex === props.stepIndex
  );
});

const handleNext = () => {
  if (props.isLastStep) {
    onboardingStore.skipTutorial();
  } else {
    onboardingStore.nextTutorial();
  }
};
</script>

<template>
  <Teleport to="body" v-if="shouldShow">
    <div
      :style="position"
      :class="[
        'fixed bg-surface border border-border/80 rounded-2xl p-5 shadow-[0_20px_50px_rgba(0,0,0,0.06)] z-50 w-[340px] transition-all duration-300 animate-in fade-in slide-in-from-top-3',
        $attrs.class,
      ]"
    >
      <div
        :style="arrowPosition"
        :class="[
          'absolute -top-1.5 w-3 h-3 bg-surface rotate-45 border-t border-l border-border/80',
          arrowClass,
        ]"
      />

      <div class="space-y-3">
        <div
          class="flex items-center space-x-2 text-primary-foreground font-semibold text-xs tracking-wider uppercase"
        >
          <SparklesIcon class="w-4 h-4 stroke-[2]" />
          <span>{{ title }}</span>
        </div>

        <div class="text-xs text-muted leading-relaxed font-normal">
          <slot />
        </div>

        <div
          class="flex justify-between items-center pt-3 border-t border-border"
        >
          <UiButton @click="onboardingStore.skipTutorial" variant="ghost">
            {{ $t("onboarding.actions.skip_tutorial") }}
          </UiButton>

          <UiButton
            variant="primary"
            @click="handleNext"
            class="flex items-center space-x-1"
          >
            <span>{{
              isLastStep
                ? $t("onboarding.actions.finish")
                : $t("onboarding.actions.continue")
            }}</span>
            <ArrowRightIcon v-if="!isLastStep" class="w-3 h-3 ml-0.5" />
          </UiButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>
