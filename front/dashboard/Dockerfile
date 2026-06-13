FROM node:26-alpine AS builder

RUN npm install -g corepack && \
    corepack enable && \
    corepack prepare pnpm@11.6.0 --activate

WORKDIR /src

ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY ./app                  ./app
COPY ./i18n                 ./i18n
COPY ./public               ./public
COPY ./nuxt.config.ts       ./nuxt.config.ts
COPY ./tailwind.config.ts   ./tailwind.config.ts
COPY ./package.json         ./package.json

RUN pnpm run build

FROM node:26-alpine AS runner

WORKDIR /src

COPY --from=builder /src/.output ./.output

ENV HOST=0.0.0.0
ENV PORT=3000
ENV NODE_ENV=production

EXPOSE 3000

CMD ["node", ".output/server/index.mjs"]
