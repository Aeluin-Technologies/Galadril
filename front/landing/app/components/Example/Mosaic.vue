<script setup>
const { t } = useI18n();

const useCases = computed(() => [
	{
		id: "energie",
		title: t("cases.energie.title"),
		image: "/illustrations/nuclear.webp",
		tags: [t("cases.energie.tags[0]")],
		description: t("cases.energie.description"),
		link: "",
	},
	{
		id: "industrie",
		title: t("cases.industrie.title"),
		image: "/illustrations/gears.webp",
		tags: [t("cases.industrie.tags[0]"), t("cases.industrie.tags[1]")],
		description: t("cases.industrie.description"),
		link: "",
	},
	{
		id: "renseignement",
		title: t("cases.renseignement.title"),
		image: "/illustrations/gulf.webp",
		description: t("cases.renseignement.description"),
		tags: [t("cases.renseignement.tags[0]"), t("cases.renseignement.tags[1]")],
		link: "",
	},
	{
		id: "supply",
		title: t("cases.supply.title"),
		image: "/illustrations/factory.webp",
		tags: [t("cases.supply.tags[0]")],
		description: t("cases.supply.description"),
		link: "",
	},
]);
</script>

<template>
	<section class="h-auto md:h-screen bg-black overflow-hidden">
		<DefaultCaseMosaic :cases="useCases" />
	</section>
</template>
