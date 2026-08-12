# syntax=docker/dockerfile:1.10

ARG NODE_IMAGE="node:26-alpine@sha256:aadf416b2cdce311a8811ba3f0608a61b77dbf997500e2eafe781b51f6a0b019"

FROM ${NODE_IMAGE} AS builder

ARG COREPACK_VERSION="0.34.0"
ARG PNPM_VERSION="11.6.0"

RUN npm install --global "corepack@${COREPACK_VERSION}" \
    && corepack enable \
    && corepack install --global "pnpm@${PNPM_VERSION}"

WORKDIR /src

ENV PNPM_HOME="/pnpm" \
    PATH="/pnpm:${PATH}"

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=galadril-dashboard-pnpm,target=/pnpm/store,sharing=locked \
    pnpm install --frozen-lockfile

COPY . .

RUN pnpm run build

FROM ${NODE_IMAGE} AS runtime

WORKDIR /src

COPY --from=builder --chown=node:node /src/.output/ ./.output/

ENV HOST="0.0.0.0" \
    NITRO_HOST="0.0.0.0" \
    PORT="3000" \
    NITRO_PORT="3000" \
    NODE_ENV="production"

EXPOSE 3000

USER node

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]

CMD ["node", ".output/server/index.mjs"]
