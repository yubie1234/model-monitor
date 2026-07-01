# model-monitor 컨테이너 이미지 (FastAPI 서비스)
# 수집 로직은 표준 라이브러리(urllib/ssl)만 쓰지만, 웹 계층은 FastAPI 스택을 쓴다.
FROM python:3.12-slim

# 이미지 메타데이터(버전은 빌드 시 --build-arg VERSION 으로 주입; ci.sh 가 채움)
ARG VERSION=dev
LABEL org.opencontainers.image.title="ai-tool/model-monitor" \
      org.opencontainers.image.description="LiteLLM/KServe/vLLM·SGLang 모델 현황 + LB 뒤 backend Pod/GPU 모니터 (FastAPI)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/yubie1234/model-monitor"

WORKDIR /app

# 사내 PyPI 프록시(Nexus) 설정 — air-gapped 빌드 시 외부 PyPI 대신 사용.
RUN pip config --global set global.trusted_host 10.20.20.123
RUN pip config --global set global.index_url http://10.20.20.123:8081/repository/pypi-proxy/simple

# 의존성 먼저 설치(레이어 캐시)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 애플리케이션 패키지
COPY app /app/app

# 비루트 실행
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser
USER 10001

EXPOSE 8088

# 기본 실행: FastAPI 서비스. 설정은 환경변수(LITELLM_BASE_URL 등) / MONITOR_CONFIG_FILE 로 주입.
#
# --timeout-keep-alive 75: nginx ingress 의 upstream keepalive(기본 60s)보다 길게 둔다.
#   uvicorn 기본은 5s 라 5초 폴링 대시보드에선 uvicorn 이 유휴 연결을 닫는 순간 nginx 가
#   그 연결을 재사용하려다 reset → 간헐 502. 업스트림(uvicorn)이 프록시(nginx)보다 늦게
#   닫게 만들어(75>60) 경합을 없앤다.
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088", \
            "--timeout-keep-alive", "75"]
