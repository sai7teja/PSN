FROM python:3.12-slim

WORKDIR /app

# Prevent Python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy dependencies first for better Docker layer caching
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/exporter.py .

# Run as non-root user for security (Kubernetes best practice)
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Start the exporter
CMD ["python", "exporter.py"]
