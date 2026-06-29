#!/usr/bin/env bash
# ci.sh — model-monitor 컨테이너 이미지 빌드 (docker build)
#
# 사용법:
#   ./ci.sh                              # ai-tool/llm-monitor:<버전> 과 :latest 빌드
#   IMAGE=<레지스트리>/ai-tool/llm-monitor ./ci.sh   # 레지스트리 경로 지정
#   TAG=test ./ci.sh                     # 태그 직접 지정
#
# 이미지 태그는 기본적으로 app/__init__.py 의 __version__ 값을 따른다.
set -euo pipefail

# 스크립트 위치를 빌드 컨텍스트로 (어디서 실행해도 동작)
cd "$(dirname "$0")"

IMAGE="${IMAGE:-ai-tool/llm-monitor}"

# 앱 버전(__version__)을 이미지 태그로 사용
VERSION="$(grep -oE '__version__ = "[^"]+"' app/__init__.py \
            | sed -E 's/.*"([^"]+)".*/\1/' || true)"
VERSION="${VERSION:-0.0.0}"
TAG="${TAG:-$VERSION}"

# 사전 점검
command -v docker >/dev/null 2>&1 || { echo "[ci] docker 명령을 찾을 수 없습니다." >&2; exit 1; }
[ -f Dockerfile ] || { echo "[ci] Dockerfile 이 없습니다." >&2; exit 1; }

echo "[ci] building image: ${IMAGE}:${TAG} (+ ${IMAGE}:latest)"

docker build \
  --build-arg "VERSION=${VERSION}" \
  -f Dockerfile \
  -t "${IMAGE}:${TAG}" \
  -t "${IMAGE}:latest" \
  .

echo "[ci] build 완료:"
docker images "${IMAGE}"
