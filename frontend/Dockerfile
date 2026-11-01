FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/tls.conf /etc/nginx/vantage/tls.conf
COPY docker/40-vantage-tls.sh /docker-entrypoint.d/
# Executable whatever the checkout kept (COPY --chmod would need BuildKit).
USER root
RUN chmod 755 /docker-entrypoint.d/40-vantage-tls.sh
USER 101
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8443
