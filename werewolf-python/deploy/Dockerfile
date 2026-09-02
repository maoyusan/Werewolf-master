FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

RUN useradd --create-home --uid 1001 --shell /usr/sbin/nologin werewolf \
    && mkdir -p /app \
    && chown werewolf:werewolf /app

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY --chown=werewolf:werewolf . /app

USER werewolf
EXPOSE 18100
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18100/healthz')"

CMD ["python", "-m", "main", "run"]
