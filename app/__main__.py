"""`python -m app` 간편 실행 래퍼 — Settings 의 host/port 로 uvicorn 을 띄운다.

운영에서는 보통 `uvicorn app.main:app --host ... --port ...` 를 직접 쓴다.
"""

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
