#!/usr/bin/env bash
#
# mirror-bundle.sh — git 히스토리를 보존하며 외부 → 내부 git 으로 옮긴다.
#
# tarball(스냅샷)과 달리 `git bundle` 은 전체 커밋/브랜치/태그를
# 단일 파일로 묶으므로, 그 파일을 내부로 옮기면 히스토리를 복원할 수 있다.
#
# [전제 — 네트워크 구조]
#   Nexus 는 "순수 프록시"다. 내부망의 GET 요청에 대해 외부 upstream 에서
#   파일을 받아 캐싱해 줄 뿐, 외부에서 Nexus 로의 업로드 경로는 없다.
#   따라서 bundle 파일은 *외부 upstream 에 정적 HTTP 파일로 미리 존재*해야
#   Nexus 가 프록시할 수 있다. 가장 현실적인 위치는 GitHub Release 에셋:
#       https://github.com/org/repo/releases/download/<tag>/repo.bundle
#   이 URL 을 Nexus raw 가 프록시하면 내부에서 받아 복원한다.
#
# 두 단계:
#   [외부]  export : 외부 repo → bundle 생성/검증 → (사람이) Release 에셋으로 게시
#   [내부]  import : Nexus 경유로 bundle 다운로드 → 히스토리 복원 → 내부 git push
#
# 사용법:
#   ./mirror-bundle.sh export <EXTERNAL_GIT_URL> <OUTPUT_BUNDLE_PATH>
#   ./mirror-bundle.sh import <RAW_PATH> <INTERNAL_GIT_URL>
#
# 예:
#   # 외부망: bundle 생성 → 이 파일을 GitHub Release 에셋으로 첨부
#   ./mirror-bundle.sh export https://github.com/org/repo.git ./repo.bundle
#
#   # 내부망: Nexus 가 프록시하는 raw 경로로 다운로드 → 복원
#   ./mirror-bundle.sh import org/repo/releases/download/v1.2.3/repo.bundle \
#       http://internal-git.local/group/repo.git
#
# 환경변수:
#   NEXUS_HOST   기본 10.20.20.123:8081
#   NEXUS_REPO   기본 raw-proxy-git
#   NEXUS_USER / NEXUS_PASS   클라이언트 → Nexus 인증(선택)
#   PUSH_MODE    mirror(기본) | refs
#       mirror : push --mirror — 내부 저장소를 bundle 과 완전 일치(내부 전용 ref 삭제)
#       refs   : push --all && push --tags — 내부 추가 ref 보존(이미 작업 중인 내부 repo용)
#
set -euo pipefail

NEXUS_HOST="${NEXUS_HOST:-10.20.20.123:8081}"
NEXUS_REPO="${NEXUS_REPO:-raw-proxy-git}"
PUSH_MODE="${PUSH_MODE:-mirror}"

# raw 저장소 안의 임의 경로 → 전체 다운로드 URL
nexus_url() { echo "http://${NEXUS_HOST}/repository/${NEXUS_REPO}/$1"; }

cmd_export() {
  local ext_url="$1" out="$2"
  local work; work="$(mktemp -d)"; trap 'rm -rf "$work"' RETURN

  echo "==> 외부 repo mirror clone: ${ext_url}"
  git clone --mirror "$ext_url" "${work}/repo.git"

  echo "==> bundle 생성 (전체 히스토리/브랜치/태그)"
  git -C "${work}/repo.git" bundle create "${work}/out.bundle" --all

  echo "==> bundle 검증"
  git -C "${work}/repo.git" bundle verify "${work}/out.bundle"

  cp -f "${work}/out.bundle" "$out"
  echo "==> 생성 완료: ${out}"
  echo "    이 파일을 외부 HTTP 위치(예: GitHub Release 에셋)에 게시하세요."
  echo "    그러면 Nexus(${NEXUS_HOST}/${NEXUS_REPO})가 GET 으로 프록시할 수 있습니다."
}

cmd_import() {
  local raw_path="$1" int_url="$2"
  local url; url="$(nexus_url "$raw_path")"
  local work; work="$(mktemp -d)"; trap 'rm -rf "$work"' RETURN

  echo "==> Nexus 경유 bundle 다운로드: ${url}"
  curl -fSL ${NEXUS_USER:+-u "${NEXUS_USER}:${NEXUS_PASS:-}"} \
    -o "${work}/repo.bundle" "$url"

  echo "==> bundle 검증"
  git bundle verify "${work}/repo.bundle"

  echo "==> bundle 에서 mirror clone (히스토리 복원)"
  git clone --mirror "${work}/repo.bundle" "${work}/repo.git"

  git -C "${work}/repo.git" remote set-url --push origin "$int_url"
  case "$PUSH_MODE" in
    mirror)
      echo "==> 내부 git push --mirror: ${int_url} (내부 전용 ref 는 삭제됨)"
      git -C "${work}/repo.git" push --mirror ;;
    refs)
      echo "==> 내부 git push --all && --tags: ${int_url} (내부 추가 ref 보존)"
      git -C "${work}/repo.git" push origin --all
      git -C "${work}/repo.git" push origin --tags ;;
    *)
      echo "알 수 없는 PUSH_MODE: ${PUSH_MODE} (mirror|refs)" >&2; exit 1 ;;
  esac

  echo "==> import 완료: ${int_url}"
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    export)
      [[ $# -eq 2 ]] || { echo "사용법: $0 export <EXTERNAL_GIT_URL> <OUTPUT_BUNDLE_PATH>" >&2; exit 1; }
      cmd_export "$@" ;;
    import)
      [[ $# -eq 2 ]] || { echo "사용법: $0 import <RAW_PATH> <INTERNAL_GIT_URL>" >&2; exit 1; }
      cmd_import "$@" ;;
    *)
      echo "사용법:" >&2
      echo "  $0 export <EXTERNAL_GIT_URL> <OUTPUT_BUNDLE_PATH>" >&2
      echo "  $0 import <RAW_PATH> <INTERNAL_GIT_URL>" >&2
      exit 1 ;;
  esac
}

main "$@"
