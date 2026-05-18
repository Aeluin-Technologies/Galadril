<script setup lang="ts">
import { ref } from "vue";
import gsap from "gsap";

const triggerWrapper = ref<HTMLElement | null>(null);
const dashboardContainer = ref<HTMLElement | null>(null); // NOUVEAU : pour cibler le déclenchement
const cardElement = ref<HTMLElement | null>(null);

const initScrollAnimation = () => {
	if (!triggerWrapper.value || !dashboardContainer.value || !cardElement.value)
		return;

	gsap.to(cardElement.value, {
		scale: 0.85,
		rotateX: 4,
		opacity: 0.8,
		ease: "none",
		scrollTrigger: {
			trigger: dashboardContainer.value,
			start: "center center",
			end: "+=100%",
			pin: triggerWrapper.value,
			pinSpacing: true,
			anticipatePin: 1,
			scrub: 1,
			invalidateOnRefresh: true,
		},
	});
};

defineExpose({ initScrollAnimation });
</script>

<template>
	<div ref="triggerWrapper" class="w-full relative overflow-visible py-12">
		<div class="flex flex-col md:flex-row gap-6 md:gap-8 mb-12">
			<div class="w-full md:w-1/4">
				<h2
					class="text-2xl md:text-3xl font-bold tracking-tight text-slate-900"
				>
					GL-AI
				</h2>
				<p
					class="font-mono text-[10px] md:text-xs font-bold tracking-widest uppercase text-amber-500 mt-2"
				>
					// {{ $t("galadril.studio.tag") }}
				</p>
			</div>
			<div class="w-full md:w-3/4 flex flex-col justify-center">
				<h3
					class="text-2xl md:text-3xl lg:text-4xl font-serif text-slate-900 leading-tight"
				>
					{{ $t("galadril.studio.title") }}
				</h3>
				<p
					class="text-slate-500 font-light mt-4 max-w-2xl text-base md:text-lg"
				>
					{{ $t("galadril.studio.desc") }}
				</p>
			</div>
		</div>

		<div ref="dashboardContainer" class="w-full style-perspective">
			<div ref="cardElement" class="w-full origin-top backend-card-shadow">
				<ExampleStudio />
			</div>
		</div>
	</div>
</template>

<style scoped>
.style-perspective {
	perspective: 1200px;
}
</style>
