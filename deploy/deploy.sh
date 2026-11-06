#!/usr/bin/env bash
# Deploys one commit on the server (#30). Run as root: deploy.sh <git sha>
# The image <ecr repo>:<sha> must already be in ECR. If the new version fails its health checks,
# this redeploys the last good one and exits non-zero.
set -euo pipefail

SHA=${1:?usage: deploy.sh <git sha>}
REGION=us-east-2
APP_DIR=/opt/asktravel
PARAMS=/asktravel/prod
HEALTH_TIMEOUT_SECONDS=180

cd "$APP_DIR"
export COMPOSE_FILE=docker-compose.prod.yml
ACCOUNT=$(aws sts get-caller-identity --region "$REGION" --query Account --output text)
REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/asktravel-api

write_env() {
  (
    umask 077
    aws ssm get-parameters-by-path --region "$REGION" --path "$PARAMS" --with-decryption \
      --query 'Parameters[].[Name,Value]' --output text |
      awk -F'\t' '{ n = split($1, part, "/"); print part[n] "=" $2 }' >.env.new
    echo "API_IMAGE=$REPO:$1" >>.env.new
    mv .env.new .env
  )
}

healthy() {
  local domain deadline
  domain=$(sed -n 's/^DOMAIN=//p' .env)
  deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  # Through Caddy, as a user would reach it, with the real certificate. --resolve keeps it on this
  # machine. The first deploy waits here while Caddy gets its certificate.
  until curl -fsS -m 10 --resolve "$domain:443:127.0.0.1" "https://$domain/agent-health" >/dev/null &&
    curl -fsS -m 10 --resolve "$domain:443:127.0.0.1" "https://$domain/health" >/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    sleep 5
  done
}

start() {
  git fetch --quiet origin
  git checkout --quiet --detach "$1"
  write_env "$1"
  docker compose pull --quiet
  docker compose up -d --remove-orphans --wait --wait-timeout "$HEALTH_TIMEOUT_SECONDS"
  healthy
}

# A safety net just before the change; the three newest are kept
if docker compose ps --status running --services 2>/dev/null | grep -q '^postgres$'; then
  mkdir -p pre-deploy
  docker compose exec -T postgres pg_dump -U travel -Fc travel_agent >"pre-deploy/$(date +%F-%H%M%S).dump"
  find pre-deploy -name '*.dump' | sort | head -n -3 | xargs -r rm --
fi

BUCKET=$(aws ssm get-parameter --region "$REGION" --name "$PARAMS/BACKUP_BUCKET" --query Parameter.Value --output text)
aws s3 sync --region "$REGION" --only-show-errors "s3://$BUCKET/guidebooks/" guidebooks/

if start "$SHA"; then
  echo "$SHA" >.last-good
  docker image prune -f >/dev/null
  echo "deployed $SHA"
  exit 0
fi

echo "deploy of $SHA failed its health checks" >&2
docker compose ps >&2 || true
if [ -f .last-good ]; then
  PREVIOUS=$(cat .last-good)
  echo "rolling back to $PREVIOUS" >&2
  start "$PREVIOUS" && echo "rolled back to $PREVIOUS" >&2 || echo "ROLLBACK FAILED" >&2
fi
exit 1
