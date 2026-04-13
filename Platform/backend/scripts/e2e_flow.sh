#!/usr/bin/env bash
set -euo pipefail

API_GATEWAY_URL="${API_GATEWAY_URL:-http://localhost:8000}"
SUPERADMIN_TOKEN="${SUPERADMIN_TOKEN:-superadmin-dev-token}"
USER_BEARER_TOKEN="${USER_BEARER_TOKEN:-user-dev-token}"
TENANT_ID="${TENANT_ID:-11111111-1111-1111-1111-111111111111}"

echo "1) Create project"
CREATE_RESPONSE=$(curl -sS -X POST "${API_GATEWAY_URL}/api/v1/projects" \
  -H "Authorization: Bearer ${USER_BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"AI Delivery MVP\",
    \"description\": \"From idea to deployment\",
    \"tenant_id\": \"${TENANT_ID}\"
  }")
echo "${CREATE_RESPONSE}"
PROJECT_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<< "${CREATE_RESPONSE}")

echo "2) Run planning"
PLANNING_RESPONSE=$(curl -sS -X POST "${API_GATEWAY_URL}/api/v1/projects/${PROJECT_ID}/planning/run" \
  -H "Authorization: Bearer ${USER_BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "idea": "Build a platform that replaces an IT team with multi-agent workflows",
    "attachments": ["landing-ref.png"],
    "tenant_id": "'"${TENANT_ID}"'"
  }')
echo "${PLANNING_RESPONSE}"

echo "3) Wait for Kafka-driven transition to INFRA_IN_PROGRESS"
for _ in $(seq 1 20); do
  STATUS_RESPONSE=$(curl -sS "${API_GATEWAY_URL}/api/v1/projects/${PROJECT_ID}/status" \
    -H "Authorization: Bearer ${USER_BEARER_TOKEN}")
  STATUS_VALUE=$(python3 -c "import json,sys; print(json.load(sys.stdin)['status'])" <<< "${STATUS_RESPONSE}")
  echo "status=${STATUS_VALUE}"
  if [ "${STATUS_VALUE}" = "INFRA_IN_PROGRESS" ]; then
    break
  fi
  sleep 1
done

echo "4) Read project status"
curl -sS "${API_GATEWAY_URL}/api/v1/projects/${PROJECT_ID}/status" \
  -H "Authorization: Bearer ${USER_BEARER_TOKEN}"
echo

echo "5) Superadmin overview"
curl -sS "${API_GATEWAY_URL}/api/v1/admin/overview" \
  -H "Authorization: Bearer ${SUPERADMIN_TOKEN}"
echo

echo "E2E flow done."
