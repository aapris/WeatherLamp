# Use Python 3.13 slim image based on Debian Trixie
FROM python:3.13-slim-trixie

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install only required system dependencies
RUN apt-get update && \
    apt-get install -y libgeos-dev && \
    rm -rf /var/lib/apt/lists/*

# Working directory; the process user is set at runtime via docker-compose
# user: "${UID}:${GID}" so that bind-mounted files are owned by the host user.
ENV HOME=/home/app
RUN mkdir -p $HOME

WORKDIR $HOME

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8000/tcp
