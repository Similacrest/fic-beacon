# FanFicFare + Calibre (calibredb) fetcher service.
# Isolated from the main app: this is the ONLY container that writes the Calibre library.
FROM python:3.12-slim

# Calibre's headless tools (calibredb) need a handful of runtime libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      calibre \
      libxml2 libxslt1.1 \
      ca-certificates \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fanficfare fastapi "uvicorn[standard]"

WORKDIR /app
COPY app.py ./app.py

# Site logins live here — mount/edit personal.ini in this container.
VOLUME ["/config", "/calibre-library"]
ENV CALIBRE_LIBRARY=/calibre-library \
    FANFICFARE_INI=/config/personal.ini
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
