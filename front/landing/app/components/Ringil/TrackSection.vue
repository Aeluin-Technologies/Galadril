<template>
	<section class="border-t border-slate-800 pt-10">
		<div class="flex flex-col md:flex-row gap-8 mb-12">
			<div class="w-full md:w-1/4">
				<h2 class="text-2xl md:text-3xl font-bold tracking-tight text-white">
					RG-TRK
				</h2>
				<p
					class="font-mono text-[10px] md:text-xs font-bold tracking-widest uppercase text-slate-500 mt-2"
				>
					// {{ $t("ringil.track.tag") }}
				</p>
			</div>
			<div class="w-full md:w-3/4 flex flex-col justify-center">
				<h3
					class="text-2xl md:text-3xl lg:text-4xl font-serif text-white leading-tight mb-4"
				>
					{{ $t("ringil.track.title") }}
				</h3>
				<p
					class="text-slate-400 font-light max-w-2xl text-base md:text-lg mb-8"
				>
					{{ $t("ringil.track.desc") }}
				</p>
				<div
					class="w-full border border-slate-800 rounded-2xl overflow-hidden bg-slate-900/20"
				>
					<ExampleTrack />
				</div>
			</div>
		</div>
	</section>
</template>
