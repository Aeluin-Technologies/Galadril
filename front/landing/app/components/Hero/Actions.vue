<template>
	<div class="flex flex-col sm:flex-row items-start gap-4">
		<NuxtLink
			to="https://github.com/RealHinome/Galadril"
			target="_blank"
			rel="noreferrer"
		>
			<button
				class="inline-flex items-center border border-amber-500 bg-amber-500 px-6 py-3 text-sm font-bold text-white hover:bg-amber-600 hover:border-amber-600 transition-colors duration-200 uppercase tracking-wide shadow-sm"
			>
				[ &rarr; ] {{ $t("galadril.hero.book_demo") }}
			</button>
		</NuxtLink>

		<NuxtLink to="https://realhinome.github.io/Galadril/studio/overview.html">
			<button
				class="inline-flex items-center border border-slate-900 px-6 py-3 text-sm font-bold text-slate-900 hover:bg-slate-900 hover:text-white transition-colors duration-200 uppercase tracking-wide"
			>
				[ + ] {{ $t("galadril.hero.connect_data") }}
			</button>
		</NuxtLink>
	</div>
</template>
