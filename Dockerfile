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

# D10 (a): download the two public policy PDFs, check them against the checksums in data/raw/MANIFEST.tsv,
# and build the index. This also downloads the embedding model into the image, so there's no download at startup.
RUN mkdir -p data/raw/policy \
 && curl -fsSL -o data/raw/policy/ndis_practice_standards_v4.pdf \
      https://www.ndiscommission.gov.au/sites/default/files/2024-10/ndis-practice-standards-and-quality-indicators.pdf \
 && curl -fsSL -o data/raw/policy/strengthened_aged_care_quality_standards.pdf \
      https://www.health.gov.au/sites/default/files/2026-08/strengthened-aged-care-quality-standards_0.pdf \
 && printf '%s  %s\n' \
      4b32e0bf1a6626b42c27d831d97ff26bca751cf15aed5fa8601657845240d09d data/raw/policy/ndis_practice_standards_v4.pdf \
      f01bd6d6ff68c364b76f8ebc1cb5ffd9a7ba7dc92324be7c6b14bf4b20c666b7 data/raw/policy/strengthened_aged_care_quality_standards.pdf \
    | sha256sum -c - \
 && python -m assistant.policy_search build

# Run as a non-root user: if the app were compromised, the attacker isn't root in the container.
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8501
CMD ["streamlit", "run", "assistant/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
