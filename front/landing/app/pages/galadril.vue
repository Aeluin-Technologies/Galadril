<script setup lang="ts">
import { onMounted, onUnmounted, ref, nextTick } from "vue";
import gsap from "gsap";
import ScrollTrigger from "gsap/ScrollTrigger";

gsap.registerPlugin(ScrollTrigger);
let ctx: gsap.Context;

const studioSectionRef = ref<any>(null);

onMounted(async () => {
	await nextTick();

	ctx = gsap.context(() => {
		// Animations d'intro
		const tl = gsap.timeline();
		tl.to(".reveal-strip", {
			scaleX: 1,
			transformOrigin: "left",
			duration: 0.6,
			ease: "power4.inOut",
			stagger: 0.1,
		})
			.to(
				".reveal-strip",
				{
					scaleX: 0,
					transformOrigin: "right",
					duration: 0.6,
					ease: "power4.inOut",
					stagger: 0.1,
				},
				"-=0.2",
			)
			.fromTo(
				".reveal-text",
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

		// 2. Lancer l'épinglage du Dashboard d'abord !
		if (studioSectionRef.value?.initScrollAnimation) {
			studioSectionRef.value.initScrollAnimation();
		}

		// 3. Appliquer l'effet d'apparition sur les ENFANTS des autres sections uniquement
		// On évite d'animer directement la section globale pour ne pas casser les coordonnées
		gsap.utils.toArray(".fade-up-section").forEach((section: any) => {
			gsap.fromTo(
				section.children,
				{ y: 40, opacity: 0 },
				{
					y: 0,
					opacity: 1,
					duration: 0.8,
					ease: "power3.out",
					scrollTrigger: {
						trigger: section,
						start: "top 85%",
						toggleActions: "play none none none",
					},
				},
			);
		});

		// 4. Forcer GSAP à recalculer les espaces créés par le PinSpacing
		ScrollTrigger.refresh();
	});
});

onUnmounted(() => {
	if (ctx) ctx.revert();
});
</script>

<template>
	<div
		class="bg-stone-50 min-h-screen font-sans text-slate-900 overflow-x-hidden"
	>
		<DefaultNavbar />

		<main class="w-full flex flex-col pt-12 pb-32">
			<div class="max-w-7xl w-full mx-auto px-6 md:px-12 mb-20">
				<HeroSection />
			</div>

			<div class="max-w-7xl w-full mx-auto px-6 md:px-12 mb-20">
				<GaladrilStudioSection ref="studioSectionRef" />
			</div>

			<div class="max-w-7xl w-full mx-auto px-6 md:px-12 flex flex-col gap-32">
				<GaladrilESKGSection class="fade-up-section" />
				<GaladrilUseCasesSection class="fade-up-section" />
				<GaladrilRingilTransition class="fade-up-section" />
			</div>
		</main>
	</div>
</template>
