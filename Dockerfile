# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

WORKDIR /app

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && mkdir -p /data \
    && chown 1000:1000 /data

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY web /app/web
COPY run.py /app/run.py

ENV DATA_DIR=/data
ENV PORT=3847
ENV PYTHONUNBUFFERED=1
EXPOSE 3847

USER 1000:1000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3847"]
