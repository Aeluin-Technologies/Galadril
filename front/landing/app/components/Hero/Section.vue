<template>
	<section class="border-t border-stone-300 pt-8 pb-10">
		<div class="flex flex-col md:flex-row gap-8 md:gap-12">
			<div class="md:w-1/4 flex flex-col justify-between">
				<div>
					<h2
						class="text-4xl md:text-5xl font-bold tracking-tight text-slate-900"
					>
						GL-01
					</h2>
					<p
						class="font-mono text-xs tracking-widest text-amber-500 uppercase font-bold mt-2"
					>
						// {{ $t("platform") }}
					</p>
				</div>
				<NuxtImg
					class="w-24 mt-8 opacity-80 mix-blend-multiply"
					alt="Galadril"
					src="/galadril.webp"
					draggable="false"
					format="webp"
				/>
			</div>

			<div class="md:w-3/4 flex flex-col items-start">
				<HeroTitle />

				<div class="relative overflow-hidden w-full mt-8 max-w-2xl">
					<div
						class="reveal-strip absolute inset-0 bg-slate-900 z-10 scale-x-0"
					></div>
					<p
						class="reveal-text text-lg md:text-xl text-slate-600 leading-relaxed font-light border-l-2 border-amber-500 pl-6 py-1"
					>
						{{ $t("galadril.hero.description") }}
					</p>
				</div>

				<div class="relative overflow-hidden w-full mt-10">
					<div
						class="reveal-strip absolute inset-0 bg-slate-900 z-10 scale-x-0"
					></div>
					<div class="reveal-text">
						<HeroActions />
					</div>
				</div>
			</div>
		</div>
	</section>
</template>
