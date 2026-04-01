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

detect_default_storage_class() {
  local default_class

  default_class="$(
    kubectl get storageclass \
      -o custom-columns=NAME:.metadata.name,DEFAULT:.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class \
      --no-headers 2>/dev/null | awk '$2 == "true" { print $1; exit }'
  )"

  if [[ -n "${default_class}" ]]; then
    printf '%s' "${default_class}"
    return
  fi

  kubectl get storageclass \
    -o custom-columns=NAME:.metadata.name,DEFAULT:.metadata.annotations.storageclass\\.beta\\.kubernetes\\.io/is-default-class \
    --no-headers 2>/dev/null | awk '$2 == "true" { print $1; exit }'
}

emit_image_pull_secrets() {
  if [[ -n "${REGISTRY_USERNAME:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
    cat <<EOF
      imagePullSecrets:
      - name: ${IMAGE_PULL_SECRET_NAME}
EOF
  fi
}

emit_volumes() {
  if [[ "${USE_PVC}" == "1" ]]; then
    cat <<EOF
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: ${PVC_NAME}
      - name: logs
        emptyDir: {}
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 1Gi
EOF
    return
  fi

  cat <<EOF
      volumes:
      - name: data
        emptyDir: {}
      - name: logs
        emptyDir: {}
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: 1Gi
EOF
}

emit_pvc_manifest() {
  cat <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NAMESPACE}
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: ${STORAGE_SIZE}
EOF

  if [[ -n "${STORAGE_CLASS}" ]]; then
    cat <<EOF
  storageClassName: ${STORAGE_CLASS}
EOF
  fi
}

emit_service_ports() {
  cat <<EOF
  - name: webui
    port: ${WEBUI_PORT}
    targetPort: webui
EOF

  if [[ "${SERVICE_TYPE}" == "NodePort" ]]; then
    cat <<EOF
    nodePort: ${NODE_PORT_WEBUI}
EOF
  fi

  cat <<EOF
  - name: novnc
    port: ${NOVNC_PORT}
    targetPort: novnc
EOF

  if [[ "${SERVICE_TYPE}" == "NodePort" ]]; then
    cat <<EOF
    nodePort: ${NODE_PORT_NOVNC}
EOF
  fi
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

WEBUI_PORT="${WEBUI_PORT:-1455}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
SERVICE_TYPE="${SERVICE_TYPE:-NodePort}"
NODE_PORT_WEBUI="${NODE_PORT_WEBUI:-31455}"
NODE_PORT_NOVNC="${NODE_PORT_NOVNC:-31080}"
REPLICAS="${REPLICAS:-1}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-Always}"
WEBUI_ACCESS_PASSWORD="${WEBUI_ACCESS_PASSWORD:-admin123}"

DEBIAN_MIRROR="${DEBIAN_MIRROR:-mirrors.aliyun.com}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"

USE_PVC="${USE_PVC:-auto}"
PVC_NAME="${PVC_NAME:-${APP_NAME}-data}"
STORAGE_CLASS="${STORAGE_CLASS:-}"
STORAGE_SIZE="${STORAGE_SIZE:-5Gi}"

REGISTRY_SERVER="${REGISTRY_SERVER:-registry.cn-shenzhen.aliyuncs.com}"
IMAGE_PULL_SECRET_NAME="${IMAGE_PULL_SECRET_NAME:-aliyun-registry}"
SKIP_PUSH="${SKIP_PUSH:-0}"

if [[ "${SERVICE_TYPE}" != "ClusterIP" && "${SERVICE_TYPE}" != "NodePort" ]]; then
  echo "SERVICE_TYPE must be ClusterIP or NodePort" >&2
  exit 1
fi

docker info >/dev/null
kubectl version --client >/dev/null
kubectl cluster-info >/dev/null

if [[ "${WEBUI_ACCESS_PASSWORD}" == "admin123" ]]; then
  log "warning: WEBUI_ACCESS_PASSWORD is still using the default value"
fi

if [[ -z "${STORAGE_CLASS}" ]]; then
  STORAGE_CLASS="$(detect_default_storage_class || true)"
fi

if [[ "${USE_PVC}" == "auto" ]]; then
  if [[ -n "${STORAGE_CLASS}" ]]; then
    USE_PVC="1"
  else
    USE_PVC="0"
  fi
fi

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

log "ensuring namespace ${NAMESPACE}"
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

log "syncing application secret"
kubectl -n "${NAMESPACE}" create secret generic "${APP_NAME}-env" \
  --from-literal=WEBUI_ACCESS_PASSWORD="${WEBUI_ACCESS_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -

if [[ -n "${REGISTRY_USERNAME:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
  log "syncing image pull secret ${IMAGE_PULL_SECRET_NAME}"
  kubectl -n "${NAMESPACE}" create secret docker-registry "${IMAGE_PULL_SECRET_NAME}" \
    --docker-server="${REGISTRY_SERVER}" \
    --docker-username="${REGISTRY_USERNAME}" \
    --docker-password="${REGISTRY_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

if [[ "${USE_PVC}" == "1" ]]; then
  log "app data will use pvc ${PVC_NAME}"
  emit_pvc_manifest | kubectl apply -f -
else
  log "no default StorageClass detected, app data will use emptyDir"
fi

log "applying deployment and service"
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
spec:
  replicas: ${REPLICAS}
  selector:
    matchLabels:
      app: ${APP_NAME}
  template:
    metadata:
      labels:
        app: ${APP_NAME}
    spec:
$(emit_image_pull_secrets)
      containers:
      - name: ${APP_NAME}
        image: ${IMAGE}
        imagePullPolicy: ${IMAGE_PULL_POLICY}
        env:
        - name: WEBUI_HOST
          value: "0.0.0.0"
        - name: WEBUI_PORT
          value: "${WEBUI_PORT}"
        - name: DISPLAY
          value: ":99"
        - name: ENABLE_VNC
          value: "1"
        - name: VNC_PORT
          value: "${VNC_PORT}"
        - name: NOVNC_PORT
          value: "${NOVNC_PORT}"
        - name: LOG_LEVEL
          value: "info"
        - name: DEBUG
          value: "0"
        envFrom:
        - secretRef:
            name: ${APP_NAME}-env
        ports:
        - name: webui
          containerPort: ${WEBUI_PORT}
        - name: novnc
          containerPort: ${NOVNC_PORT}
        - name: vnc
          containerPort: ${VNC_PORT}
        readinessProbe:
          httpGet:
            path: /
            port: webui
          initialDelaySeconds: 20
          periodSeconds: 10
        livenessProbe:
          tcpSocket:
            port: webui
          initialDelaySeconds: 60
          periodSeconds: 20
        volumeMounts:
        - name: data
          mountPath: /app/data
        - name: logs
          mountPath: /app/logs
        - name: dshm
          mountPath: /dev/shm
$(emit_volumes)
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
spec:
  type: ${SERVICE_TYPE}
  selector:
    app: ${APP_NAME}
  ports:
$(emit_service_ports)
EOF

log "waiting for rollout"
kubectl -n "${NAMESPACE}" rollout status deployment/"${APP_NAME}" --timeout="${ROLLOUT_TIMEOUT}"

log "current pods"
kubectl -n "${NAMESPACE}" get pods -l app="${APP_NAME}" -o wide

log "current service"
kubectl -n "${NAMESPACE}" get service "${APP_NAME}" -o wide

if [[ "${SERVICE_TYPE}" == "NodePort" ]]; then
  log "webui:  http://<node-ip>:${NODE_PORT_WEBUI}"
  log "noVNC:  http://<node-ip>:${NODE_PORT_NOVNC}"
else
  log "service type is ClusterIP, use port-forward if you need local access"
  log "kubectl -n ${NAMESPACE} port-forward svc/${APP_NAME} ${WEBUI_PORT}:${WEBUI_PORT} ${NOVNC_PORT}:${NOVNC_PORT}"
fi
