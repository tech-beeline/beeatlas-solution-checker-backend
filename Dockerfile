ARG RUN_IMAGE=ubuntu:22.04
FROM ${RUN_IMAGE}

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates python3 pip python3-dev && \
    rm -rf /var/cache/apt/archives /var/lib/apt/lists/* && \
    groupadd --gid 1000 app && \
    useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --chmod=644 certs/* /usr/local/share/ca-certificates/
RUN update-ca-certificates

ARG PIP_INDEX_URL=''
ENV PIP_INDEX_URL=$PIP_INDEX_URL
ARG PIP_TRUSTED_HOST=''
ENV PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app /app/app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache/pip

USER 1000:1000

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
