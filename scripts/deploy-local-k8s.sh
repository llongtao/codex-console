#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() {
  printf '[deploy-local-k8s] %s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

detect_container_name() {
  local container_names
  local container_count

  container_names="$(
    kubectl -n "${NAMESPACE}" get deployment "${APP_NAME}" \
      -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\n"}{end}'
  )"

  if [[ -z "${container_names}" ]]; then
    echo "deployment ${APP_NAME} has no containers" >&2
    exit 1
  fi

  if grep -Fxq "${APP_NAME}" <<<"${container_names}"; then
    printf '%s' "${APP_NAME}"
    return
  fi

  container_count="$(wc -l <<<"${container_names}" | tr -d ' ')"
  if [[ "${container_count}" == "1" ]]; then
    printf '%s' "${container_names}"
    return
  fi

  echo "failed to determine target container for deployment ${APP_NAME}" >&2
  echo "containers:" >&2
  printf '%s\n' "${container_names}" >&2
  exit 1
}

require_cmd docker
require_cmd kubectl
require_cmd git

NAMESPACE="${NAMESPACE:-app}"
APP_NAME="${APP_NAME:-codex-console}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:-registry.cn-shenzhen.aliyuncs.com/longtao}"
IMAGE_NAME="${IMAGE_NAME:-${APP_NAME}}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)}"
IMAGE="${REGISTRY_PREFIX}/${IMAGE_NAME}:${IMAGE_TAG}"

ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"

DEBIAN_MIRROR="${DEBIAN_MIRROR:-mirrors.aliyun.com}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"

SKIP_PUSH="${SKIP_PUSH:-0}"

docker info >/dev/null
kubectl version --client >/dev/null
kubectl cluster-info >/dev/null

log "building image ${IMAGE}"
docker build \
  --build-arg APT_MIRROR="${DEBIAN_MIRROR}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL}" \
  --build-arg PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST}" \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST}" \
  -t "${IMAGE}" \
  "${ROOT_DIR}"

if [[ "${SKIP_PUSH}" != "1" ]]; then
  log "pushing image ${IMAGE}"
  docker push "${IMAGE}"
else
  log "skipping push because SKIP_PUSH=1"
fi

log "checking deployment ${APP_NAME} in namespace ${NAMESPACE}"
kubectl -n "${NAMESPACE}" get deployment "${APP_NAME}" >/dev/null

CONTAINER_NAME="$(detect_container_name)"

log "updating deployment/${APP_NAME} container ${CONTAINER_NAME} to ${IMAGE}"
kubectl -n "${NAMESPACE}" set image deployment/"${APP_NAME}" "${CONTAINER_NAME}=${IMAGE}"

log "waiting for rollout"
kubectl -n "${NAMESPACE}" rollout status deployment/"${APP_NAME}" --timeout="${ROLLOUT_TIMEOUT}"

log "current pods"
kubectl -n "${NAMESPACE}" get pods -l app="${APP_NAME}" -o wide
