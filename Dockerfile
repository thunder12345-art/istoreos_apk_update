FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY auto/ /app/auto/

HEALTHCHECK --interval=5m --timeout=15s --start-period=90s --retries=3 \
    CMD python -c "import json,os,time; s=json.load(open(os.environ.get('STATE_FILE','/data/state.json'))); i=float(os.environ.get('CHECK_INTERVAL_SECONDS','86400')); raise SystemExit(0 if time.time()-float(s.get('last_check_ts') or 0) <= max(2*i,1800) else 1)"

CMD ["python", "/app/auto/apk_updater.py"]
