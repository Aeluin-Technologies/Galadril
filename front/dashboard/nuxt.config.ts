import { isDevelopment } from "std-env";

export default defineNuxtConfig({
  $env: {
    "ui-test": {
      devtools: { enabled: false },
      nitro: { externals: { chmod: true } },
    },
  },
  telemetry: false,
  css: [
    "katex/dist/katex.min.css",
    "highlight.js/styles/github.css",
    "~/assets/css/pengolo.css",
    "~/assets/css/administration.css",
  ],
  compatibilityDate: "2025-07-15",
  builder: "vite",
  vite: {
    optimizeDeps: {
      include: [
        "grid-layout-plus",
        "vue-echarts",
        "@heroicons/vue/24/outline",
        "graphql-tag",
        "@vueuse/core",
      ],
    },
  },
  devtools: { enabled: isDevelopment },
  ssr: false,
  colorMode: { preference: "light", fallback: "light" },

  app: {
    keepalive: true,
    head: {
      charset: "utf-8",
      viewport: "width=device-width,initial-scale=1",
      title: "Galadril Dashboard",
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
        { property: "og:site_name", content: "Galadril Dashboard" },
        { property: "og:title", content: "Galadril Dashboard" },
        {
          property: "og:description",
          content:
            "Galadril provides advanced data integration and AI platform for real-time complex systems.",
        },
        { property: "og:image", content: "/favicon.png" },
      ],
    },
  },

  sourcemap: isDevelopment,
  devServer: {
    port: 3000,
  },

  modules: [
    "@pinia/nuxt",
    "@nuxtjs/apollo",
    "@nuxt/a11y",
    "@nuxt/eslint",
    "@nuxt/hints",
    "@nuxt/image",
    "@nuxtjs/color-mode",
    "@nuxtjs/i18n",
    "@galadril/ui/nuxt",
    "nuxt-echarts",
    ...(isDevelopment ? [] : ["nuxt-security"]),
  ],

  pinia: {
    storesDirs: ["./app/stores/**"],
  },

  apollo: {
    autoImports: true,
    clients: {
      default: "./apollo/default.ts",
    },
  },

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
    ],
    baseUrl: "",
  },

  echarts: {
    ssr: false,
    renderer: ["canvas", "svg"],
    charts: [
      "BarChart",
      "LineChart",
      "PieChart",
      "CandlestickChart",
      "GaugeChart",
      "GraphChart",
      "MapChart",
      "ScatterChart",
      "LinesChart",
    ],
    components: [
      "DatasetComponent",
      "GridComponent",
      "TooltipComponent",
      "LegendComponent",
      "GeoComponent",
    ],
  },

  routeRules: {
    "/login": {
      security: {
        headers: {
          permissionsPolicy: {
            "publickey-credentials-get": ["self"],
            "publickey-credentials-create": ["self"],
          },
        },
      },
    },
    "/auth/**": {
      security: {
        rateLimiter: {
          tokensPerInterval: 50,
          interval: 60000,
          ipHeader: "x-envoy-external-address",
          headers: true,
        },
        requestSizeLimiter: {
          maxRequestSizeInBytes: 8192,
          maxUploadFileRequestInBytes: 8192,
        },
      },
    },
  },

  nitro: {
    preset: "node-server",
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
      referrerPolicy: "same-origin",
      strictTransportSecurity: {
        maxAge: 63072000, // 2 years
        includeSubdomains: true,
        preload: true,
      },
      xFrameOptions: "DENY",
      contentSecurityPolicy: {
        "font-src": ["'self'", "data:"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
        "frame-src": ["'self'"],
        "worker-src": ["none"],
        "connect-src": [
          "'self'",
          "https:",
          "wss:",
          "localhost:8080",
          "ws://localhost:8080",
        ],
        "img-src": ["'self'", "https:", "data:", "blob:"],
        "media-src": ["'self'", "https:"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "upgrade-insecure-requests": false,
      },
      permissionsPolicy: {
        "publickey-credentials-get": ["self"],
        "publickey-credentials-create": ["self"],
        camera: [],
        geolocation: [],
        microphone: [],
        "sync-xhr": [],
      },
    },
    corsHandler: {
      methods: ["OPTIONS", "GET", "POST"],
      allowHeaders: ["Authorization", "Content-Type", "Accept"],
      credentials: false,
      maxAge: "86400",
      preflight: {
        statusCode: 200,
      },
    },
    allowedMethodsRestricter: {
      methods: ["OPTIONS", "GET", "POST"],
    },
    rateLimiter: false,
  },
});
