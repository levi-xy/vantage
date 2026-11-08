FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# The map editor's icon library (dashboard-icons, Apache-2.0), pinned and checksummed by
# icons.manifest.json; served by this image, never fetched by the browser from a CDN.
RUN node scripts/fetch-icons.mjs
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
LABEL org.opencontainers.image.title="Vantage frontend" \
      org.opencontainers.image.description="Vantage web UI (nginx, HTTPS)" \
      org.opencontainers.image.url="https://github.com/levi-xy/vantage" \
      org.opencontainers.image.source="https://github.com/levi-xy/vantage" \
      org.opencontainers.image.licenses="Apache-2.0"
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
