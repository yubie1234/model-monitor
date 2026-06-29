"""in-cluster Kubernetes API 클라이언트 (표준 라이브러리 urllib + ssl 만 사용).

api_base(LB 주소) 뒤에 실제 몇 개의 Pod 이 떠 있는지는 LB 를 probe 해서는 알 수
없고, 컨트롤 플레인(k8s API)에 물어봐야 한다. 그 호출을 담당하는 얇은 클라이언트.
"""

import os
import ssl
import json
import urllib.error
import urllib.request


class K8sClient:
    """in-cluster Kubernetes API 를 표준 라이브러리만으로 호출."""

    def __init__(self, api_server, token, ssl_ctx, timeout, default_namespace):
        self.api_server = api_server.rstrip("/") if api_server else None
        self.token = token
        self.ssl_ctx = ssl_ctx
        self.timeout = timeout
        self.default_namespace = default_namespace or "default"

    @property
    def enabled(self):
        return bool(self.api_server)

    @classmethod
    def from_settings(cls, settings):
        """in-cluster ServiceAccount 토큰/CA 가 있으면 활성, 없으면 None."""
        if not settings.get("backend_count"):
            return None

        # API server 주소: 명시 > env > 기본 in-cluster DNS
        api_server = settings.get("k8s_api_server")
        if not api_server:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if host:
                api_server = "https://%s:%s" % (host, port)

        token = None
        token_file = settings.get("k8s_token_file")
        if token_file and os.path.exists(token_file):
            try:
                with open(token_file) as f:
                    token = f.read().strip()
            except OSError:
                token = None

        # 토큰이 없으면 클러스터 밖(개발환경)으로 보고 k8s API 비활성
        if not token:
            api_server = None

        ssl_ctx = None
        if api_server:
            if settings.get("k8s_insecure"):
                ssl_ctx = ssl._create_unverified_context()
            else:
                ca = settings.get("k8s_ca_file")
                try:
                    if ca and os.path.exists(ca):
                        ssl_ctx = ssl.create_default_context(cafile=ca)
                    else:
                        ssl_ctx = ssl.create_default_context()
                except Exception:
                    ssl_ctx = ssl._create_unverified_context()

        # 네임스페이스 폴백 최종값: 설정 > SA namespace 파일 > default
        ns = settings.get("default_namespace")
        if not ns:
            ns_file = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
            if os.path.exists(ns_file):
                try:
                    with open(ns_file) as f:
                        ns = f.read().strip()
                except OSError:
                    ns = None

        if not api_server:   # 토큰/주소 없으면(클러스터 밖) backend 개수 수집 불가
            return None
        return cls(
            api_server=api_server, token=token, ssl_ctx=ssl_ctx,
            timeout=settings.get("k8s_timeout", 5.0), default_namespace=ns,
        )

    def get(self, path):
        """k8s API GET -> (ok, data, err)."""
        if not self.api_server:
            return False, None, "k8s api server not configured"
        url = self.api_server + path
        headers = {"Accept": "application/json",
                   "Authorization": "Bearer %s" % self.token}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                return True, json.loads(resp.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            return False, None, "HTTP %s %s" % (e.code, e.reason)
        except Exception as e:  # noqa: BLE001
            return False, None, "%s: %s" % (type(e).__name__, e)
