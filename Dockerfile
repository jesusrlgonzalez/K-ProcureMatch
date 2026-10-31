FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    KPM_DATA_DIR=/app/data/sample KPM_DB=/app/state/kpm.duckdb KPM_UPLOAD_DIR=/app/state/uploads
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY rules ./rules
COPY dashboard ./dashboard
COPY data/sample ./data/sample
RUN mkdir /app/state && chown app:app /app/state
USER app
EXPOSE 8000 8501
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
# Por defecto la API. El dashboard (servicio "dashboard" en compose) la consume vía KPM_API_URL.
CMD ["uvicorn", "procurematch.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
