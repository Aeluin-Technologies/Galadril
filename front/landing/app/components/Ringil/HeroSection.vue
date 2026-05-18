<template>
	<section class="border-t border-slate-800 pt-8 pb-20 relative">
		<!-- Drone Image Watermark -->
		<div
			class="absolute right-0 bottom-0 w-full md:w-2/3 h-full pointer-events-none overflow-hidden opacity-20 mix-blend-luminosity grayscale z-0"
		>
			<NuxtImg
				src="/illustrations/drone.webp"
				alt="Drone Schematic"
				class="w-full h-full object-contain object-right-bottom scale-110 translate-y-10"
				draggable="false"
			/>
			<div
				class="absolute inset-0 bg-gradient-to-t from-black via-transparent to-transparent"
			></div>
			<div
				class="absolute inset-0 bg-gradient-to-r from-black via-transparent to-transparent"
			></div>
		</div>

		<div class="flex flex-col md:flex-row gap-8 md:gap-12 relative z-10">
			<div class="md:w-1/4 flex flex-col justify-between">
				<div>
					<h2 class="text-4xl md:text-5xl font-bold tracking-tight text-white">
						RG-01
					</h2>
					<p
						class="font-mono text-xs tracking-widest text-slate-500 uppercase font-bold mt-2"
					>
						// {{ $t("ringil.hero.tag") }}
					</p>
				</div>
			</div>

			<div class="md:w-3/4 flex flex-col items-start">
				<h1
					class="text-4xl md:text-5xl lg:text-6xl font-serif tracking-tight text-white leading-[1.1] flex flex-col gap-2"
				>
					<div class="relative overflow-hidden inline-block w-full">
						<div
							class="reveal-strip absolute inset-0 bg-slate-700 z-10 scale-x-0"
						></div>
						<span class="reveal-text inline-block pb-1">{{
							$t("ringil.hero.title_p1")
						}}</span>
					</div>
					<div class="relative overflow-hidden inline-block w-full">
						<div
							class="reveal-strip absolute inset-0 bg-slate-700 z-10 scale-x-0"
						></div>
						<span class="reveal-text inline-block pb-1 text-slate-400 italic">{{
							$t("ringil.hero.title_p2")
						}}</span>
					</div>
				</h1>

				<div class="relative overflow-hidden w-full mt-8 max-w-2xl">
					<div
						class="reveal-strip absolute inset-0 bg-slate-700 z-10 scale-x-0"
					></div>
					<p
						class="reveal-text text-lg md:text-xl text-slate-400 leading-relaxed font-light border-l-2 border-slate-700 pl-6 py-1"
					>
						{{ $t("ringil.hero.description") }}
					</p>
				</div>

				<div class="relative overflow-hidden w-full mt-10">
					<div
						class="reveal-strip absolute inset-0 bg-slate-700 z-10 scale-x-0"
					></div>
					<div class="reveal-text flex flex-col sm:flex-row items-start gap-4">
						<NuxtLink
							to="https://github.com/RealHinome/Ringil"
							target="_blank"
							rel="noreferrer"
						>
							<button
								class="inline-flex items-center border border-blue-500 bg-blue-500 px-6 py-3 text-sm font-bold text-white hover:bg-blue-600 hover:border-blue-600 transition-colors duration-200 uppercase tracking-wide shadow-sm"
							>
								[ &rarr; ] {{ $t("ringil.hero.discover") }}
							</button>
						</NuxtLink>

						<NuxtLink to="/ringil.pdf" rel="noreferrer" external>
							<button
								class="inline-flex items-center border border-slate-700 bg-white/5 px-6 py-3 text-sm font-bold text-white hover:bg-white hover:text-black hover:border-white transition-colors duration-200 uppercase tracking-wide shadow-sm"
							>
								[ + ] {{ $t("ringil.hero.specification") }}
							</button>
						</NuxtLink>
					</div>
				</div>
			</div>
		</div>
	</section>
</template>
