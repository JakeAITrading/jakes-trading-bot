FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
EXPOSE 8080

# One worker so the in-memory daily trade limits are shared; threads for concurrency.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 120 app:app
