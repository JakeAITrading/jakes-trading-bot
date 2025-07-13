# Use official Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port (for Cloud Run)
ENV PORT=8080
EXPOSE 8080

# Start app
CMD ["python", "app.py"]
