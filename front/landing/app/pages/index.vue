<script setup lang="ts">
import gsap from "gsap";

let ctx: gsap.Context;

const strips = ref<HTMLElement[]>([]);
const texts = ref<HTMLElement[]>([]);

const addToStrips = (el: any) => {
	if (el) strips.value.push(el);
};
const addToTexts = (el: any) => {
	if (el) texts.value.push(el);
};

onMounted(() => {
	ctx = gsap.context(() => {
		const tl = gsap.timeline();

		tl.to(strips.value, {
			scaleX: 1,
			transformOrigin: "left",
			duration: 0.6,
			ease: "power4.inOut",
			stagger: 0.15,
		})
			.to(
				strips.value,
				{
					scaleX: 0,
					transformOrigin: "right",
					duration: 0.6,
					ease: "power4.inOut",
					stagger: 0.15,
				},
				"-=0.2",
			)
			.fromTo(
				texts.value,
				{ y: "100%", opacity: 0 },
				{
					y: "0%",
					opacity: 1,
					duration: 0.6,
					ease: "power4.out",
					stagger: 0.15,
				},
				"-=0.9",
			);
	});
});

onUnmounted(() => {
	if (ctx) ctx.revert();
});
</script>

<template>
	<div
		class="min-h-screen bg-stone-50 text-slate-900 font-sans overflow-x-hidden selection:bg-sky-900 selection:text-white"
	>
		<main class="max-w-7xl mx-auto px-6 md:px-12 pt-20 pb-32">
			<div class="mb-24 md:w-3/4">
				<p
					class="font-mono text-sm tracking-widest uppercase text-sky-900 mb-6 font-bold"
				>
					// {{ $t("home.company") }}
				</p>

				<h1
					class="text-3xl md:text-5xl lg:text-6xl font-serif leading-[1.1] tracking-tight text-slate-900 flex flex-col gap-2"
				>
					<div class="relative overflow-hidden inline-block w-full">
						<div
							:ref="addToStrips"
							class="absolute inset-0 bg-slate-900 z-10 scale-x-0"
						></div>
						<span :ref="addToTexts" class="inline-block pb-1">{{
							$t("home.hero.title_p1")
						}}</span>

						<div
							:ref="addToStrips"
							class="absolute inset-0 bg-slate-900 z-10 scale-x-0"
						></div>
						<span
							:ref="addToTexts"
							class="inline-block pb-1 text-sky-900 italic"
							>{{ $t("home.hero.title_p2") }}</span
						>
					</div>
					<div class="relative overflow-hidden inline-block w-full">
						<div
							:ref="addToStrips"
							class="absolute inset-0 bg-slate-900 z-10 scale-x-0"
						></div>
						<span :ref="addToTexts" class="inline-block pb-1">{{
							$t("home.hero.title_p3")
						}}</span>
					</div>
					<div class="relative overflow-hidden inline-block w-full">
						<div
							:ref="addToStrips"
							class="absolute inset-0 bg-slate-900 z-10 scale-x-0"
						></div>
						<span :ref="addToTexts" class="inline-block pb-1">{{
							$t("home.hero.title_p4")
						}}</span>
					</div>
				</h1>
			</div>

			<div class="mb-20">
				<div
					class="border-t-2 border-sky-900 flex flex-col md:flex-row pt-8 pb-16 gap-8 md:gap-12"
				>
					<div class="md:w-1/4">
						<h2
							class="text-4xl md:text-5xl font-bold tracking-tight text-slate-900"
						>
							GL-01
						</h2>
					</div>
					<div class="md:w-3/4 flex flex-col items-start">
						<h3
							class="text-4xl md:text-5xl lg:text-6xl font-bold tracking-tight text-slate-900 mb-8 leading-[1.05]"
						>
							{{ $t("home.galadril_section.title") }}
						</h3>
						<NuxtLink
							to="/galadril"
							class="inline-flex items-center border border-sky-900 px-5 py-2.5 text-sm font-bold text-slate-900 hover:bg-sky-900 hover:text-white transition-colors duration-200"
						>
							[ &rarr; ] {{ $t("home.galadril_section.cta") }}
						</NuxtLink>
					</div>
				</div>
				<div
					class="border-t border-sky-900/40 flex flex-col md:flex-row py-5 gap-8 md:gap-12 text-sm font-bold text-slate-700"
				>
					<div class="md:w-1/4">{{ $t("home.galadril_section.version") }}</div>
					<div class="md:w-3/4 flex gap-4 items-center">
						{{ $t("home.galadril_section.status") }}
					</div>
				</div>
				<div class="border-t-2 border-sky-900"></div>
			</div>

			<div class="mb-10">
				<div
					class="border-t-2 border-sky-900 flex flex-col md:flex-row pt-8 pb-16 gap-8 md:gap-12"
				>
					<div class="md:w-1/4">
						<h2
							class="text-4xl md:text-5xl font-bold tracking-tight text-slate-900"
						>
							RG-01
						</h2>
					</div>
					<div class="md:w-3/4 flex flex-col items-start">
						<h3
							class="text-4xl md:text-5xl lg:text-6xl font-bold tracking-tight text-slate-900 mb-8 leading-[1.05]"
						>
							{{ $t("home.ringil_section.title") }}
						</h3>
						<NuxtLink
							to="/ringil"
							class="inline-flex items-center border border-sky-900 px-5 py-2.5 text-sm font-bold text-slate-900 hover:bg-sky-900 hover:border-slate-900 hover:text-white transition-colors duration-200"
						>
							[ &rarr; ] {{ $t("home.ringil_section.cta") }}
						</NuxtLink>
					</div>
				</div>
				<div
					class="border-t border-sky-900/40 flex flex-col md:flex-row py-5 gap-8 md:gap-12 text-sm font-bold text-slate-700"
				>
					<div class="md:w-1/4">{{ $t("home.ringil_section.version") }}</div>
					<div class="md:w-3/4 flex gap-4 items-center">
						{{ $t("home.ringil_section.status") }}
					</div>
				</div>
				<div class="border-t-2 border-sky-900"></div>
			</div>
		</main>
	</div>
</template>
