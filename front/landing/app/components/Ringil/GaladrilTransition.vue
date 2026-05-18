<template>
	<section class="mt-10 md:mt-16">
		<div
			class="bg-stone-50 rounded-3xl md:rounded-[2rem] p-6 sm:p-10 md:p-16 flex flex-col lg:flex-row gap-8 lg:gap-12 relative overflow-hidden shadow-2xl"
		>
			<div
				class="absolute top-0 right-0 w-64 md:w-96 h-64 md:h-96 bg-amber-500/20 blur-[80px] md:blur-[100px] pointer-events-none"
			></div>

			<div
				class="w-full lg:w-1/2 relative z-10 flex flex-col items-start justify-center"
			>
				<div
					class="inline-flex items-center gap-2 px-3 py-1 mb-4 md:mb-6 border border-slate-300 bg-white rounded-full text-[10px] md:text-xs font-mono tracking-widest text-slate-500 uppercase"
				>
					<div
						class="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"
					></div>
					{{ $t("ringil.galadril_transition.tag") }}
				</div>
				<h3
					class="text-2xl sm:text-3xl md:text-5xl font-serif text-slate-900 leading-tight mb-4 md:mb-6"
				>
					{{ $t("ringil.galadril_transition.title_p1") }} <br />
					<span class="text-amber-500 italic">{{
						$t("ringil.galadril_transition.title_p2")
					}}</span>
				</h3>
				<p
					class="text-sm md:text-base text-slate-600 font-light mb-6 md:mb-8 max-w-md"
				>
					{{ $t("ringil.galadril_transition.desc") }}
				</p>
				<NuxtLink to="/galadril" class="w-full sm:w-auto">
					<button
						class="w-full sm:w-auto inline-flex justify-center items-center border border-amber-500 bg-amber-500 px-5 md:px-6 py-3 text-xs md:text-sm font-bold text-white hover:bg-amber-600 hover:border-amber-600 transition-colors duration-200 uppercase tracking-wide"
					>
						[ &larr; ] {{ $t("ringil.galadril_transition.cta") }}
					</button>
				</NuxtLink>
			</div>

			<div
				class="w-full lg:w-1/2 relative z-10 flex items-center justify-center mt-6 lg:mt-0"
			>
				<div
					class="w-full bg-white rounded-2xl p-4 md:p-6 shadow-xl border border-slate-200 flex flex-col gap-3 md:gap-4"
				>
					<div
						class="flex flex-wrap items-center justify-between pb-3 md:pb-4 border-b border-slate-100 gap-2"
					>
						<div class="flex items-center gap-3">
							<div
								class="w-8 h-8 md:w-10 md:h-10 rounded-full bg-blue-100 flex items-center justify-center text-blue-600 shrink-0"
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									class="h-4 w-4 md:h-5 md:w-5"
									viewBox="0 0 20 20"
									fill="currentColor"
								>
									<path
										d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"
									/>
								</svg>
							</div>
							<div>
								<p class="font-bold text-slate-900 text-xs md:text-sm">
									Galadril Insights
								</p>
								<p class="text-[10px] md:text-xs text-slate-500">
									Swarm Telemetry Processed
								</p>
							</div>
						</div>
						<span class="text-[10px] md:text-xs font-mono text-slate-400"
							>Live</span
						>
					</div>
					<div
						class="bg-slate-50 rounded-lg p-3 md:p-4 text-xs md:text-sm text-slate-700"
					>
						<p class="mb-2">
							<strong>Causal Link Established:</strong> Swarm data correlates
							humidity drop to pipeline micro-fracture. Mapping risk to broader
							supply chain.
						</p>
						<div
							class="flex items-center gap-2 mt-2 md:mt-3 text-blue-600 font-medium"
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								class="h-3 w-3 md:h-4 w-4"
								viewBox="0 0 20 20"
								fill="currentColor"
							>
								<path
									fill-rule="evenodd"
									d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
									clip-rule="evenodd"
								/>
							</svg>
							Dashboard metrics updated.
						</div>
					</div>
				</div>
			</div>
		</div>
	</section>
</template>
