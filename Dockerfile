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

# One worker is enough; the app keeps no state in memory between requests.
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
