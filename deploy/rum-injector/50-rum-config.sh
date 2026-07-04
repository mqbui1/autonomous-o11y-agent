#!/bin/sh
# Substitute only our env vars — leaves nginx's $host, $remote_addr, etc. intact
envsubst '${SPLUNK_REALM} ${SPLUNK_RUM_TOKEN} ${SPLUNK_ENVIRONMENT}' \
  < /tmp/nginx.conf.template \
  > /etc/nginx/conf.d/default.conf
