#!/usr/bin/env bash
set -euo pipefail

# Set this to your DuckDNS subdomain (without .duckdns.org)
DUCKDNS_DOMAIN="daythree-ai"

TOKEN_FILE="/opt/duckdns/token"
LOG_FILE="/opt/duckdns/duckdns.log"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "Missing token file: $TOKEN_FILE" | tee -a "$LOG_FILE"
  exit 1
fi

TOKEN="$(cat "$TOKEN_FILE" | tr -d '\r\n')"
if [[ -z "$TOKEN" ]]; then
  echo "DuckDNS token is empty in $TOKEN_FILE" | tee -a "$LOG_FILE"
  exit 1
fi

DATE_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# DuckDNS returns: OK or KO
RESULT="$(curl -fsS "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${TOKEN}&ip=" || true)"
echo "${DATE_UTC} ${RESULT}" >> "$LOG_FILE"

