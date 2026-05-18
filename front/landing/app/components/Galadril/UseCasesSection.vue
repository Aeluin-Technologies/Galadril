<template>
	<section class="border-t border-stone-300 pt-10">
		<div class="flex flex-col md:flex-row gap-8 mb-12">
			<div class="md:w-1/4">
				<h2 class="text-3xl font-bold tracking-tight text-slate-900">GL-USE</h2>
				<p
					class="font-mono text-xs font-bold tracking-widest uppercase text-amber-500 mt-2"
				>
					// {{ $t("galadril.use_cases.tag") }}
				</p>
			</div>
			<div class="md:w-3/4 flex flex-col justify-center">
				<h3
					class="text-3xl md:text-4xl font-serif text-slate-900 leading-tight"
				>
					{{ $t("galadril.use_cases.title") }}
				</h3>
			</div>
		</div>
		<ExampleMosaic />
	</section>
</template>
