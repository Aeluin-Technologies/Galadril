<template>
	<section class="border-t border-slate-800 pt-10">
		<div class="flex flex-col md:flex-row gap-8 mb-12">
			<div class="w-full md:w-1/4">
				<h2 class="text-2xl md:text-3xl font-bold tracking-tight text-white">
					RG-SWM
				</h2>
				<p
					class="font-mono text-[10px] md:text-xs font-bold tracking-widest uppercase text-slate-500 mt-2"
				>
					// {{ $t("ringil.bento.tag") }}
				</p>
			</div>
			<div class="w-full md:w-3/4">
				<div class="grid grid-cols-1 md:grid-cols-2 gap-4 md:gap-6">
					<div
						class="col-span-1 bg-slate-900/40 border border-slate-800 rounded-2xl p-8 flex flex-col justify-between hover:border-slate-600 transition-colors relative overflow-hidden group"
					>
						<div class="relative z-10">
							<h3 class="text-xl font-serif text-white mb-2">
								{{ $t("ringil.bento.agility.title") }}
							</h3>
							<p class="text-sm text-slate-400">
								{{ $t("ringil.bento.agility.desc") }}
							</p>
						</div>
						<div
							class="text-5xl md:text-6xl font-light tracking-tighter text-white mt-6 relative z-10"
						>
							80<span
								class="text-xl text-slate-500 font-mono tracking-normal ml-1"
								>km/h</span
							>
						</div>
					</div>

					<div
						class="col-span-1 bg-slate-900/40 border border-slate-800 rounded-2xl p-8 flex flex-col justify-between hover:border-slate-600 transition-colors relative overflow-hidden group"
					>
						<NuxtImg
							src="/illustrations/nvidia-jetson.webp"
							class="absolute -right-10 -bottom-10 w-64 h-64 object-contain opacity-20 grayscale group-hover:opacity-30 group-hover:scale-105 transition-all duration-700 ease-out z-0"
							draggable="false"
							alt="Hardware Processing Unit"
						/>
						<div
							class="absolute inset-0 bg-gradient-to-t from-slate-900/80 via-transparent to-transparent z-0"
						></div>

						<div class="relative z-10">
							<h3 class="text-xl font-serif text-white mb-2">
								{{ $t("ringil.bento.intelligence.title") }}
							</h3>
							<p class="text-sm text-slate-400 max-w-[80%]">
								{{ $t("ringil.bento.intelligence.desc") }}
							</p>
						</div>
						<div
							class="text-5xl md:text-6xl font-light tracking-tighter text-white mt-6 relative z-10"
						>
							<span
								class="text-xl text-slate-500 font-mono tracking-normal mr-1"
								>Lat:</span
							>12<span
								class="text-xl text-slate-500 font-mono tracking-normal ml-1"
								>ms</span
							>
						</div>
					</div>

					<div
						class="col-span-1 md:col-span-2 bg-slate-900/40 border border-slate-800 rounded-2xl p-8 flex flex-col md:flex-row items-center gap-8 hover:border-slate-600 transition-colors"
					>
						<div class="md:w-1/2">
							<h3 class="text-xl font-serif text-white mb-2">
								{{ $t("ringil.bento.scale.title") }}
							</h3>
							<p class="text-sm text-slate-400">
								{{ $t("ringil.bento.scale.desc") }}
							</p>
						</div>
						<div class="md:w-1/2 w-full">
							<div
								class="bg-black border border-slate-800 rounded-xl p-5 font-mono text-xs text-slate-500 w-full shadow-inner"
							>
								<p>
									<span class="text-blue-400">root@swarm:</span>~$ push
									firmware_v2.1
								</p>
								<p class="text-slate-300 mt-1">✓ bit-to-bit match confirmed</p>
								<p class="mt-1">syncing 1,042 entities...</p>
							</div>
						</div>
					</div>
				</div>
			</div>
		</div>
	</section>
</template>
