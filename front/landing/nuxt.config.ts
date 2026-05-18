import { isDevelopment } from "std-env";

export default defineNuxtConfig({
	compatibilityDate: "2025-07-15",
	builder: "vite",
	devtools: { enabled: true },

	app: {
		keepalive: true,
		head: {
			charset: "utf-8",
			viewport: "width=device-width,initial-scale=1",
			title: "Aeluin",
			link: [
				{ rel: "icon", type: "image/webp", href: "/favicon.webp" },
				{ rel: "apple-touch-icon", href: "/favicon.webp" },
			],
			meta: [
				{
					property: "apple-mobile-web-app-status-bar-style",
					content: "default",
				},
				{ property: "og:type", content: "website" },
				{ property: "og:site_name", content: "Aeluin" },
				{ property: "og:title", content: "Aeluin" },
				{
					property: "og:description",
					content:
						"Aeluin provides advanced data integration and AI platform for real-time complex systems.",
				},
				{ property: "og:image", content: "/favicon.png" },
			],
		},
	},

	sourcemap: isDevelopment,

	modules: [
		"@nuxt/a11y",
		"@nuxt/eslint",
		"@nuxt/hints",
		"@nuxt/image",
		"@nuxtjs/color-mode",
		"@nuxtjs/i18n",
		"@nuxtjs/tailwindcss",
		...(isDevelopment ? [] : ["nuxt-security"]),
	],

	i18n: {
		defaultLocale: "en",
		strategy: "no_prefix",
		langDir: ".",
		detectBrowserLanguage: {
			useCookie: true,
			cookieKey: "locale",
			redirectOn: "root",
			fallbackLocale: "en",
			alwaysRedirect: true,
		},
		locales: [
			{
				code: "en",
				iso: "en-US",
				file: "en-US.json",
				name: "English",
			},
			{
				code: "fr",
				iso: "fr-FR",
				file: "fr-FR.json",
				name: "Français",
			},
		],
		baseUrl: "https://aeluin.gravitalia.com",
	},

	routeRules: {
		"/": {
			noScripts: false,
		},
	},

	nitro: {
		preset: isDevelopment ? "node-server" : "cloudflare_module",
		prerender: {
			autoSubfolderIndex: !isDevelopment,
		},
	},

	sri: true,
	security: {
		strict: true,
		hidePoweredBy: true,
		removeLoggers: true,
		headers: {
			crossOriginEmbedderPolicy: "credentialless",
			crossOriginOpenerPolicy: "same-origin",
			crossOriginResourcePolicy: "same-site",
			originAgentCluster: "?1",
			referrerPolicy: "no-referrer",
			strictTransportSecurity: {
				maxAge: 63072000, // 2 years
				includeSubdomains: true,
				preload: true,
			},
			xFrameOptions: "DENY", // also managed by CSP.
			contentSecurityPolicy: {
				"font-src": ["'none'"],
				"form-action": ["'none'"],
				"frame-ancestors": ["'none'"],
				"frame-src": ["'none'"],
				"worker-src": ["none"],
				"connect-src": ["'self'", "https:", "localhost:8080"],
				"img-src": ["'self'", "https:", "data:", "blob:"],
				"media-src": ["'self'", "https:"],
				"style-src": ["'self'", "'nonce-{{nonce}}'"],
				"upgrade-insecure-requests": false,
			},
			permissionsPolicy: {
				camera: [],
				geolocation: [],
				microphone: [],
				"sync-xhr": [],
			},
		},
		corsHandler: {
			methods: ["OPTIONS", "GET"],
			allowHeaders: ["Authorization", "Content-Type", "Accept"],
			credentials: false,
			maxAge: "86400",
			preflight: {
				statusCode: 200,
			},
		},
		allowedMethodsRestricter: {
			methods: ["OPTIONS", "GET"],
		},
		rateLimiter: false,
	},
});
