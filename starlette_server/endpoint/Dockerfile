# Use Python 3.13 slim image based on Debian Trixie
FROM python:3.13-slim-trixie

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install only required system dependencies
RUN apt-get update && \
    apt-get install -y libgeos-dev && \
    rm -rf /var/lib/apt/lists/*

# Create and set up the app user
ENV HOME=/home/app
RUN mkdir -p $HOME && \
    addgroup --system app && \
    adduser --system --group app && \
    chown -R app:app $HOME

WORKDIR $HOME

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=app:app . .

USER app

EXPOSE 8000/tcp
