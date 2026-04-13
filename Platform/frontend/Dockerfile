FROM node:20-alpine AS build

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

# Empty = same-origin; nginx proxies /api and /metrics to api-gateway
ARG VITE_GATEWAY_BASE=
ENV VITE_GATEWAY_BASE=$VITE_GATEWAY_BASE

RUN npm run build

FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
