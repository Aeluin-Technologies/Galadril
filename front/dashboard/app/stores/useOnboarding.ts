import { defineStore } from "pinia";

export const useOnboardingStore = defineStore("onboarding", {
  state: () => ({
    hasIngestedData: false,
    hasCreatedPipeline: false,
    hasBuiltDashboard: false,
    currentTutorialStepIndex: 0,
    isTutorialCompleted: false,
  }),

  actions: {
    hydrate() {
      if (import.meta.client) {
        const data = localStorage.getItem("galadril_onboarding_state");
        if (data) {
          try {
            const parsed = JSON.parse(data);
            this.hasIngestedData = parsed.hasIngestedData ?? false;
            this.hasCreatedPipeline = parsed.hasCreatedPipeline ?? false;
            this.hasBuiltDashboard = parsed.hasBuiltDashboard ?? false;
            this.currentTutorialStepIndex =
              parsed.currentTutorialStepIndex ?? 0;
            this.isTutorialCompleted = parsed.isTutorialCompleted ?? false;
          } catch (e) {
            console.error("Onboarding error:", e);
          }
        }
      }
    },

    persist() {
      if (import.meta.client) {
        localStorage.setItem(
          "galadril_onboarding_state",
          JSON.stringify({
            hasIngestedData: this.hasIngestedData,
            hasCreatedPipeline: this.hasCreatedPipeline,
            hasBuiltDashboard: this.hasBuiltDashboard,
            currentTutorialStepIndex: this.currentTutorialStepIndex,
            isTutorialCompleted: this.isTutorialCompleted,
          }),
        );
      }
    },

    setStepCompleted(step: "ingest" | "pipeline" | "dashboard") {
      if (step === "ingest") this.hasIngestedData = true;
      if (step === "pipeline") this.hasCreatedPipeline = true;
      if (step === "dashboard") this.hasBuiltDashboard = true;
      this.persist();
    },

    nextTutorial() {
      this.currentTutorialStepIndex++;
      this.persist();
    },

    skipTutorial() {
      this.isTutorialCompleted = true;
      this.persist();
    },
  },
});
