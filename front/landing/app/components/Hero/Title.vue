<template>
	<h1
		class="text-4xl md:text-5xl lg:text-6xl font-serif tracking-tight text-slate-900 leading-[1.1] flex flex-col gap-2"
	>
		<div class="relative overflow-hidden inline-block w-full">
			<div
				class="reveal-strip absolute inset-0 bg-slate-900 z-10 scale-x-0"
			></div>
			<span class="reveal-text inline-block pb-1">{{
				$t("galadril.hero.title_p1")
			}}</span>
		</div>
		<div class="relative overflow-hidden inline-block w-full">
			<div
				class="reveal-strip absolute inset-0 bg-slate-900 z-10 scale-x-0"
			></div>
			<span class="reveal-text inline-block pb-1 text-amber-500 italic">{{
				$t("galadril.hero.title_p2")
			}}</span>
		</div>
	</h1>
</template>
