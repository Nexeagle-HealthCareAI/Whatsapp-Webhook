# Pinned to -bookworm (Debian 12) explicitly — plain "slim" floats to whatever Debian
# release is current (trixie as of writing), which Microsoft's msodbcsql18 repo below
# doesn't have a matching config for.
FROM python:3.12-slim-bookworm

WORKDIR /app

# ODBC Driver 18 for SQL Server (Microsoft's own apt repo) — needed by pyodbc/aioodbc for
# this bot's own conversation_state/processed_messages/pending_appointments tables. The
# prod.list below ships its own `signed-by=/usr/share/keyrings/microsoft-prod.gpg` clause,
# so the key must be dearmored to that exact binary keyring path — trusted.gpg.d (ASCII-armored)
# is ignored once a source line specifies signed-by.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg unixodbc-dev \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list | tee /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY worker.py .
COPY scheduler.py .
COPY sender.py .
COPY conversation_logger.py .

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
