<script setup lang="ts">
const props = defineProps<{
	cases: Array<{
		title: string;
		subtitle?: string;
		image: string;
		description?: string;
		tags?: string[];
		link?: string;
		id?: string;
	}>;
}>();

const emit = defineEmits<{
	(e: "open", id: string): void;
}>();

function handleClick(item: any) {
	if (item.link) {
		window.open(item.link, "_blank");
	} else if (item.id) {
		emit("open", item.id);
	}
}

const getGridClass = (i: number) => {
	switch (i) {
		case 0:
			return "col-span-2 md:col-span-2 md:row-span-2 aspect-[4/3] md:aspect-auto h-full";
		case 1:
			return "col-span-1 md:col-span-1 md:row-span-1 aspect-square md:aspect-auto h-full";
		case 2:
			return "col-span-1 md:col-span-1 md:row-span-1 aspect-square md:aspect-auto h-full";
		case 3:
			return "col-span-2 md:col-span-2 md:row-span-1 aspect-[2/1] md:aspect-auto h-full";
		default:
			return "col-span-1 h-full";
	}
};
</script>

<template>
	<div
		class="grid grid-cols-2 md:grid-cols-4 md:grid-rows-2 gap-[1px] bg-stone-200 w-full h-full border border-stone-200"
	>
		<div
			v-for="(item, i) in cases.slice(0, 4)"
			:key="item.id ?? item.title"
			class="group relative cursor-pointer overflow-hidden bg-stone-100"
			:class="getGridClass(i)"
			@click="handleClick(item)"
		>
			<NuxtImg
				:src="item.image"
				class="absolute inset-0 w-full h-full object-cover z-0 opacity-90 mix-blend-luminosity grayscale group-hover:grayscale-0 group-hover:scale-105 transition-all duration-700 ease-out"
			/>

			<!-- Gradient overlay for text readability -->
			<div
				class="absolute inset-0 bg-gradient-to-t from-slate-900/80 via-slate-900/20 to-transparent z-1"
			></div>

			<div
				class="relative z-10 flex flex-col justify-end h-full p-6 md:p-8 pointer-events-none"
			>
				<div class="flex flex-wrap gap-2 mb-3" v-if="item.tags">
					<span
						v-for="tag in item.tags"
						:key="tag"
						class="text-[10px] uppercase tracking-widest font-mono font-bold text-amber-500"
					>
						// {{ tag }}
					</span>
				</div>

				<h3
					class="text-xl md:text-2xl font-bold text-white tracking-tight leading-tight"
				>
					{{ item.title }}
				</h3>

				<div
					v-if="item.description"
					class="hidden sm:block text-xs md:text-sm text-stone-300 mt-3 line-clamp-2 max-w-md border-l-2 border-amber-500 pl-3"
				>
					{{ item.description }}
				</div>
			</div>
		</div>
	</div>
</template>
