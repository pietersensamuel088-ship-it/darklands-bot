FROM python:3.12-slim

# Prevent python from writing pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project code and configuration
COPY src/ ./src/
COPY config.json .

# Create data directory for database persistence
RUN mkdir -p /app/data

CMD ["python", "src/bot.py"]
