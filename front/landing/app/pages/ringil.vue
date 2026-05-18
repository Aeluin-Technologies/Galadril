<script setup lang="ts">
import gsap from "gsap";
import ScrollTrigger from "gsap/ScrollTrigger";

gsap.registerPlugin(ScrollTrigger);

let ctx: gsap.Context;

const introStrips = ref<HTMLElement[]>([]);
const introTexts = ref<HTMLElement[]>([]);
const fadeSections = ref<HTMLElement[]>([]);

const setFadeSectionRef = (el: any) => {
	if (!el) return;

	const element = el.$el ? el.$el : el;

	if (!fadeSections.value.includes(element)) {
		fadeSections.value.push(element);
	}
};

onMounted(async () => {
	await nextTick();

	ctx = gsap.context(() => {
		const tl = gsap.timeline();

		if (introStrips.value.length > 0) {
			tl.to(introStrips.value, {
				scaleX: 1,
				transformOrigin: "left",
				duration: 0.6,
				ease: "power4.inOut",
				stagger: 0.1,
			}).to(
				introStrips.value,
				{
					scaleX: 0,
					transformOrigin: "right",
					duration: 0.6,
					ease: "power4.inOut",
					stagger: 0.1,
				},
				"-=0.2",
			);
		}

		if (introTexts.value.length > 0) {
			tl.fromTo(
				introTexts.value,
				{ y: "100%", opacity: 0 },
				{
					y: "0%",
					opacity: 1,
					duration: 0.6,
					ease: "power4.out",
					stagger: 0.1,
				},
				"-=0.8",
			);
		}

		fadeSections.value.forEach((section: HTMLElement) => {
			if (!section) return;
			gsap.fromTo(
				section.children,
				{ y: 30, opacity: 0 },
				{
					y: 0,
					opacity: 1,
					duration: 0.8,
					ease: "power3.out",
					stagger: 0.05,
					scrollTrigger: {
						trigger: section,
						start: "top 85%",
						toggleActions: "play none none none",
					},
				},
			);
		});

		ScrollTrigger.refresh();
	});
});

onUnmounted(() => {
	if (ctx) ctx.revert();
});
</script>

<template>
	<div
		class="bg-black min-h-screen font-sans text-white overflow-x-hidden selection:bg-slate-700 selection:text-white relative"
	>
		<DefaultNavbar brand="Ringil" theme="dark" class="!relative" />

		<div
			class="absolute top-0 left-1/2 -translate-x-1/2 w-[800px] h-[800px] bg-blue-800/20 blur-[150px] rounded-full pointer-events-none z-0"
		></div>

		<main
			class="max-w-7xl mx-auto px-6 md:px-12 pt-12 pb-32 flex flex-col gap-20 relative z-10"
		>
			<RingilHeroSection />

			<RingilBentoSection :ref="setFadeSectionRef" />
			<RingilMeshSection :ref="setFadeSectionRef" />
			<RingilTrackSection :ref="setFadeSectionRef" />
			<RingilGaladrilTransition :ref="setFadeSectionRef" />
		</main>
	</div>
</template>
