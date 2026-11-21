FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-login --gecos "" --uid 1000 --home /home/appuser appuser \
    && mkdir -p /data \
    && chown 1000:1000 /data

WORKDIR /opt/casa
COPY app/ /opt/casa/

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Bangkok

USER 1000:1000
EXPOSE 8795
CMD ["python", "-u", "server.py"]
