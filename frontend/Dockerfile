# The build output is plain JS/CSS, the same for every platform: build it natively on the
# runner (an emulated arm64 npm ci hung for an hour in the release job), and only the
# nginx stage below is per-platform.
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# The release version shown in the UI, passed by the release workflow ("dev" otherwise).
ARG VANTAGE_VERSION=dev
ARG VANTAGE_BUILD_DATE=
ENV VANTAGE_VERSION=${VANTAGE_VERSION} VANTAGE_BUILD_DATE=${VANTAGE_BUILD_DATE}
# The map editor's icon library (dashboard-icons, Apache-2.0), pinned and checksummed by
# icons.manifest.json; served by this image, never fetched by the browser from a CDN.
RUN node scripts/fetch-icons.mjs
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
LABEL org.opencontainers.image.title="Vantage frontend" \
      org.opencontainers.image.description="Vantage web UI (nginx, HTTPS)" \
      org.opencontainers.image.url="https://github.com/levi-xy/vantage" \
      org.opencontainers.image.source="https://github.com/levi-xy/vantage" \
      org.opencontainers.image.licenses="MIT"
# The license and notice (the repository root, passed as the "license" build context).
COPY --from=license LICENSE NOTICE /usr/share/doc/vantage/
COPY third-party/dashboard-icons/LICENSE /usr/share/doc/vantage/dashboard-icons-LICENSE
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/tls.conf /etc/nginx/vantage/tls.conf
COPY docker/40-vantage-tls.sh /docker-entrypoint.d/
# Executable whatever the checkout kept (COPY --chmod would need BuildKit).
USER root
RUN chmod 755 /docker-entrypoint.d/40-vantage-tls.sh
USER 101
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8443
