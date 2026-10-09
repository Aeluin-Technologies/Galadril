# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b

ARG TIMESCALEDB_IMAGE="timescale/timescaledb-ha:pg18.6-ts2.30.2-all@sha256:e3f8a0a03a0c12a5948f9f1aff99ae671d1591766d3995c69901fb65e118ba8f"

FROM ${TIMESCALEDB_IMAGE} AS age-builder

ARG AGE_COMMIT="e43dc1a12b78fba4acef9835b2b10379b8d243b4"

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    bison \
    build-essential \
    ca-certificates \
    flex \
    git \
    postgresql-server-dev-18 \
    && rm -rf /var/lib/apt/lists/*

RUN git init /tmp/age \
    && git -C /tmp/age remote add origin https://github.com/apache/age.git \
    && git -C /tmp/age fetch --depth 1 origin "${AGE_COMMIT}" \
    && git -C /tmp/age checkout --detach FETCH_HEAD \
    && make -C /tmp/age -j"$(nproc)" OPTFLAGS="-O3" \
    && make -C /tmp/age OPTFLAGS="-O3" install

FROM ${TIMESCALEDB_IMAGE} AS runtime

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    postgresql-18-postgis-3 \
    postgresql-18-cron \
    postgresql-18-pg-wait-sampling \
    postgresql-18-repack \
    postgresql-plpython3-18 \
    python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=age-builder /usr/lib/postgresql/18/lib/age.so /usr/lib/postgresql/18/lib/
COPY --from=age-builder /usr/share/postgresql/18/extension/age* /usr/share/postgresql/18/extension/

COPY --chown=postgres:postgres --chmod=0555 docker-entrypoint-initdb.d/ /docker-entrypoint-initdb.d/

USER postgres

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD pg_isready --quiet --username="${POSTGRES_USER:-postgres}" --dbname="${POSTGRES_DB:-postgres}"

CMD ["postgres", "-c", "listen_addresses=*", "-c", "shared_preload_libraries=timescaledb,age,pg_cron,pg_stat_statements,pg_wait_sampling"]
