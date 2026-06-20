FROM python:3.11-slim

WORKDIR /app

COPY artifacts/notha/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY artifacts/notha/ .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
