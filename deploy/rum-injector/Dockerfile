FROM nginx:1.25-alpine

# Remove default site config
RUN rm /etc/nginx/conf.d/default.conf

COPY nginx.conf.template /tmp/nginx.conf.template
COPY 50-rum-config.sh /docker-entrypoint.d/50-rum-config.sh
RUN chmod +x /docker-entrypoint.d/50-rum-config.sh
