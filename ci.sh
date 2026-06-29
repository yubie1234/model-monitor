#!/usr/bin/env bash
# ci.sh — model-monitor 컨테이너 이미지 빌드 (docker build)
#
# 사용법:
#   ./ci.sh                              # product 빌드: <버전> + :latest
#   BRANCH=develop ./ci.sh               # <버전>-develop (:latest 안 붙임)
#   BRANCH=product ./ci.sh               # <버전> + :latest (미지정과 동일)
#   IMAGE=<레지스트리>/ai-tool/model-monitor ./ci.sh   # 레지스트리 경로 지정
#   TAG=test ./ci.sh                     # 태그 직접 지정(BRANCH 로직 무시)
#
# 이미지 태그는 기본적으로 app/__init__.py 의 __version__ 값을 따른다.
# BRANCH 로 브랜치별 태그를 구분한다:
#   - 미지정 또는 BRANCH=product -> <버전>           (+ :latest)
#   - 그 외 값(develop, any ...)  -> <버전>-<BRANCH>  (:latest 안 붙임)
set -euo pipefail

# 스크립트 위치를 빌드 컨텍스트로 (어디서 실행해도 동작)
cd "$(dirname "$0")"

IMAGE="${IMAGE:-ai-tool/model-monitor}"

# 앱 버전(__version__)을 이미지 태그로 사용
VERSION="$(grep -oE '__version__ = "[^"]+"' app/__init__.py \
            | sed -E 's/.*"([^"]+)".*/\1/' || true)"
VERSION="${VERSION:-0.0.0}"

# 브랜치별 태그: product(또는 미지정)는 suffix 없음, 그 외는 -<BRANCH>.
# docker 태그에 못 쓰는 문자(/ 공백 등)는 '-' 로 치환하고 끝 '-' 는 제거.
BRANCH_RAW="${BRANCH:-}"
BRANCH_SAN="$(printf '%s' "$BRANCH_RAW" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-*$//')"
if [ -z "$BRANCH_RAW" ] || [ "$BRANCH_RAW" = "product" ]; then
  VTAG="${VERSION}"; IS_PRODUCT=1
else
  VTAG="${VERSION}-${BRANCH_SAN}"; IS_PRODUCT=0
fi
# TAG 를 직접 주면 그대로(브랜치 로직 무시). 안 주면 위 VTAG 사용.
TAG="${TAG:-$VTAG}"

# 사전 점검
command -v docker >/dev/null 2>&1 || { echo "[ci] docker 명령을 찾을 수 없습니다." >&2; exit 1; }
[ -f Dockerfile ] || { echo "[ci] Dockerfile 이 없습니다." >&2; exit 1; }

# product(또는 미지정)일 때만 :latest 도 같이 붙인다(develop 등이 latest 를 덮지 않게).
build_tags=( -t "${IMAGE}:${TAG}" )
[ "$IS_PRODUCT" = "1" ] && build_tags+=( -t "${IMAGE}:latest" )

echo "[ci] building image: ${IMAGE}:${TAG}$([ "$IS_PRODUCT" = "1" ] && echo " (+ ${IMAGE}:latest)")"

docker build \
  --network=host \
  --build-arg "VERSION=${VTAG}" \
  -f Dockerfile \
  "${build_tags[@]}" \
  .

echo "[ci] build 완료:"
docker images "${IMAGE}"
