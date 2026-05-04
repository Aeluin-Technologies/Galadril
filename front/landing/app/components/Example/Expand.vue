<template>
	<section
		class="image-section h-[100svh] bg-gray-900 flex items-center justify-center relative overflow-hidden"
	>
		<h3
			class="absolute top-24 md:top-20 w-full text-3xl md:text-5xl font-serif text-center z-20 text-white mix-blend-difference pointer-events-none px-4"
		>
			{{ $t("expand.deployed_everywhere") }} <br /><span
				class="italic text-gray-300"
				>{{ $t("expand.secure") }}</span
			>
		</h3>

		<div
			class="expand-image absolute z-10 w-[80vw] md:w-[40vw] h-[40vh] md:h-[50vh] rounded-[16px] md:rounded-[24px] overflow-hidden shadow-2xl"
		>
			<NuxtImg
				src="/illustrations/gulf.webp"
				format="webp"
				width="500"
				draggable="false"
				alt=""
				class="w-full h-full object-cover opacity-80"
			/>
		</div>
	</section>
</template>
