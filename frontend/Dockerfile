# syntax=docker/dockerfile:1
#
# Frontend image: build with Node, serve the static bundle with nginx.

FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build


FROM nginx:1.27-alpine AS runtime
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
# 127.0.0.1 rather than localhost: the latter prefers ::1 in this image.
HEALTHCHECK --interval=15s --timeout=4s --start-period=8s --retries=4 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]
