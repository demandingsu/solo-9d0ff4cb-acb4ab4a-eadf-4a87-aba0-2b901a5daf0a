FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY samples ./samples

EXPOSE 8000

# uvicorn workers share the same PostgreSQL database; concurrency safety is
# provided by advisory locks + the field_versions optimistic token.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
