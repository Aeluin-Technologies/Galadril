<template>
	<section class="border-t border-slate-800 pt-10">
		<div class="flex flex-col md:flex-row gap-8 mb-12">
			<div class="w-full md:w-1/4">
				<h2 class="text-2xl md:text-3xl font-bold tracking-tight text-white">
					RG-NET
				</h2>
				<p
					class="font-mono text-[10px] md:text-xs font-bold tracking-widest uppercase text-slate-500 mt-2"
				>
					// {{ $t("ringil.mesh.tag") }}
				</p>
			</div>
			<div class="w-full md:w-3/4 flex flex-col md:flex-row items-center gap-8">
				<div
					class="w-full md:w-1/2 aspect-square relative bg-slate-900/20 rounded-full border border-slate-800 flex items-center justify-center overflow-hidden"
				>
					<NuxtImg
						class="w-3/4 h-3/4 opacity-40 grayscale mix-blend-screen"
						src="/illustrations/mesh.svg"
						draggable="false"
						alt="Mesh Network"
					/>
				</div>
				<div class="w-full md:w-1/2">
					<h3
						class="text-2xl md:text-3xl font-serif text-white leading-tight mb-4"
					>
						{{ $t("ringil.mesh.title") }}
					</h3>
					<p class="text-slate-400 font-light text-base md:text-lg">
						{{ $t("ringil.mesh.desc") }}
					</p>
				</div>
			</div>
		</div>
	</section>
</template>
