<script setup lang="ts">
import { SparklesIcon, ArrowRightIcon } from "@heroicons/vue/24/outline";
import { useOnboardingStore } from "~/stores/useOnboarding";

const props = defineProps<{
  stepIndex: number;
  isLastStep?: boolean;
  title: string;
  arrowClass?: string;
}>();

const onboardingStore = useOnboardingStore();

onMounted(() => {
  onboardingStore.hydrate();
});

const shouldShow = computed(() => {
  return (
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
      :class="[
        'fixed bg-white border border-zinc-200/80 rounded-2xl p-5 shadow-[0_20px_50px_rgba(0,0,0,0.06)] z-50 w-[340px] transition-all duration-300 animate-in fade-in slide-in-from-top-3',
        $attrs.class,
      ]"
    >
      <div
        :class="[
          'absolute -top-1.5 w-3 h-3 bg-white rotate-45 border-t border-l border-zinc-200/80',
          arrowClass,
        ]"
      />

      <div class="space-y-3">
        <div
          class="flex items-center space-x-2 text-amber-600 font-semibold text-xs tracking-wider uppercase"
        >
          <SparklesIcon class="w-4 h-4 stroke-[2]" />
          <span>{{ title }}</span>
        </div>

        <div class="text-xs text-zinc-600 leading-relaxed font-normal">
          <slot />
        </div>

        <div
          class="flex justify-between items-center pt-3 border-t border-zinc-100"
        >
          <button
            @click="onboardingStore.skipTutorial"
            class="text-[11px] font-medium text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            {{ $t("onboarding.actions.skip_tutorial") }}
          </button>

          <UtilsButton
            variant="primary"
            class="!py-1.5 !px-3 !text-xs font-medium !bg-amber-600 hover:!bg-amber-700 !text-white flex items-center space-x-1 rounded-lg shadow-sm"
            @click="handleNext"
          >
            <span>{{
              isLastStep
                ? $t("onboarding.actions.finish")
                : $t("onboarding.actions.continue")
            }}</span>
            <ArrowRightIcon v-if="!isLastStep" class="w-3 h-3 ml-0.5" />
          </UtilsButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>
