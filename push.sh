#!/usr/bin/env bash
# push.sh — ci.sh 로 빌드한 이미지를 사내 레지스트리에 push
#
# 사용법:
#   ./ci.sh && ./push.sh         # 빌드 후 push (<버전> 과 latest)
#   TAG=test ./push.sh           # 특정 태그만
#
# 레지스트리는 10.92.20.77:5002 로 고정.
# 주의: HTTP(비TLS) 레지스트리라면 docker daemon / 노드 containerd 에
#       insecure-registries 설정(10.92.20.77:5002)이 있어야 push/pull 된다.
set -euo pipefail

cd "$(dirname "$0")"

REPO_URL="10.92.20.77:5002"            # 고정 레지스트리
IMAGE="${IMAGE:-ai-tool/llm-monitor}"  # ci.sh 가 빌드한 로컬 이미지명

# 빌드와 동일하게 __version__ 을 태그로 사용
VERSION="$(grep -oE '__version__ = "[^"]+"' model_monitor.py \
            | sed -E 's/.*"([^"]+)".*/\1/' || true)"
VERSION="${VERSION:-0.0.0}"
TAG="${TAG:-$VERSION}"

command -v docker >/dev/null 2>&1 || { echo "[push] docker 명령을 찾을 수 없습니다." >&2; exit 1; }

# 로컬 이미지 존재 확인 (먼저 ci.sh 로 빌드해야 함)
docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1 || {
  echo "[push] 로컬 이미지 ${IMAGE}:${TAG} 가 없습니다. 먼저 ./ci.sh 로 빌드하세요." >&2
  exit 1
}

# <버전> 과 latest 둘 다 retag 후 push
for t in "${TAG}" latest; do
  src="${IMAGE}:${t}"
  dst="${REPO_URL}/${IMAGE}:${t}"
  docker image inspect "${src}" >/dev/null 2>&1 || { echo "[push] skip (없음): ${src}"; continue; }
  echo "[push] ${src}  ->  ${dst}"
  docker tag "${src}" "${dst}"
  docker push "${dst}"
done

echo "[push] 완료:"
echo "  ${REPO_URL}/${IMAGE}:${TAG}"
echo "  ${REPO_URL}/${IMAGE}:latest"
