FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static static
COPY templates templates

RUN useradd --system --uid 1000 timelog && mkdir /data && chown timelog /data
USER timelog
ENV TIMELOG_DB=/data/timelog.db
VOLUME /data
EXPOSE 8000

# One worker only: open timer streams are tracked in memory (TimerHub in app.py).
# Open timer streams never end by themselves, so a restart waits at most 3 seconds for them.
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--timeout-graceful-shutdown", "3"]
