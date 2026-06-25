# model-monitor 컨테이너 이미지
# 외부 패키지 0개(표준 라이브러리만) -> slim 베이스에 스크립트 한 개만 복사.
FROM python:3.12-slim

# 이미지 메타데이터(버전은 빌드 시 --build-arg VERSION 으로 주입; ci.sh 가 채움)
ARG VERSION=dev
LABEL org.opencontainers.image.title="model-monitor" \
      org.opencontainers.image.description="LiteLLM/KServe/vLLM·SGLang 모델 현황 + LB 뒤 backend Pod 개수 모니터" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/yubie1234/llm-monitor"

WORKDIR /app
COPY model_monitor.py /app/model_monitor.py

# 비루트 실행
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser
USER 10001

EXPOSE 8088

ENTRYPOINT ["python3", "/app/model_monitor.py"]
# 기본은 도움말. k8s/실행 시 args 로 --serve 등을 덮어쓴다.
CMD ["--help"]
