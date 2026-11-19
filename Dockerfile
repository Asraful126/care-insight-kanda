# Care Insights assistant: Streamlit chat app with the policy search index built into the image.
# Pinned to Debian 12 (bookworm): the Microsoft ODBC repo below is the Debian 12 one.
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/Asraful126/care-insight-kanda"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed

# Microsoft ODBC Driver 18: the assistant's SQL tool talks to the Fabric SQL endpoint through it.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY assistant/ assistant/

# D10 (b): the policy index is committed in data/index/ (rebuilt locally with
# `python -m assistant.policy_search build` when a PDF changes). The government sites stall downloads from
# CI runners, so the image doesn't fetch the PDFs. The embedding model is still downloaded here, at build
# time, so the container needs no download at startup.
COPY data/index/ data/index/
RUN python -c "from assistant.policy_search import _embedder; list(_embedder().embed(['warm-up']))"

# Run as a non-root user: if the app were compromised, the attacker isn't root in the container.
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8501
CMD ["streamlit", "run", "assistant/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
