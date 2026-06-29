#!/usr/bin/env bash
# push.sh — ci.sh 로 빌드한 이미지를 사내 레지스트리에 push
#
# 사용법:
#   ./ci.sh && ./push.sh                       # product: <버전> + latest
#   BRANCH=develop ./ci.sh && BRANCH=develop ./push.sh   # <버전>-develop (latest 제외)
#   TAG=test ./push.sh                         # 특정 태그만(BRANCH 로직 무시)
#
# ci.sh 와 동일한 BRANCH 규칙으로 태그를 정한다(빌드와 push 에 같은 BRANCH 를 넘길 것):
#   - 미지정/product -> <버전>           (+ latest)
#   - 그 외 값        -> <버전>-<BRANCH>  (latest 제외)
#
# 레지스트리는 10.92.20.77:5002 로 고정.
# 주의: HTTP(비TLS) 레지스트리라면 docker daemon / 노드 containerd 에
#       insecure-registries 설정(10.92.20.77:5002)이 있어야 push/pull 된다.
set -euo pipefail

cd "$(dirname "$0")"

REPO_URL="10.92.20.77:5002"            # 고정 레지스트리
IMAGE="${IMAGE:-ai-tool/model-monitor}"  # ci.sh 가 빌드한 로컬 이미지명

# 빌드와 동일하게 __version__ + BRANCH 규칙으로 태그를 정한다(ci.sh 와 일치).
VERSION="$(grep -oE '__version__ = "[^"]+"' app/__init__.py \
            | sed -E 's/.*"([^"]+)".*/\1/' || true)"
VERSION="${VERSION:-0.0.0}"

BRANCH_RAW="${BRANCH:-}"
BRANCH_SAN="$(printf '%s' "$BRANCH_RAW" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-*$//')"
if [ -z "$BRANCH_RAW" ] || [ "$BRANCH_RAW" = "product" ]; then
  VTAG="${VERSION}"; IS_PRODUCT=1
else
  VTAG="${VERSION}-${BRANCH_SAN}"; IS_PRODUCT=0
fi
TAG="${TAG:-$VTAG}"

command -v docker >/dev/null 2>&1 || { echo "[push] docker 명령을 찾을 수 없습니다." >&2; exit 1; }

# 로컬 이미지 존재 확인 (먼저 ci.sh 로 빌드해야 함)
docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1 || {
  echo "[push] 로컬 이미지 ${IMAGE}:${TAG} 가 없습니다. 먼저 ./ci.sh 로 빌드하세요." >&2
  exit 1
}

# 태그 목록: 버전 태그(+ product 면 latest) retag 후 push.
push_tags=( "${TAG}" )
[ "$IS_PRODUCT" = "1" ] && push_tags+=( latest )
for t in "${push_tags[@]}"; do
  src="${IMAGE}:${t}"
  dst="${REPO_URL}/${IMAGE}:${t}"
  docker image inspect "${src}" >/dev/null 2>&1 || { echo "[push] skip (없음): ${src}"; continue; }
  echo "[push] ${src}  ->  ${dst}"
  docker tag "${src}" "${dst}"
  docker push "${dst}"
done

echo "[push] 완료:"
for t in "${push_tags[@]}"; do echo "  ${REPO_URL}/${IMAGE}:${t}"; done
