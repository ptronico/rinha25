# Rinha25 API

https://github.com/zanfranceschi/rinha-de-backend-2025

A FastAPI-based REST API built with Python 3.13.

## Features

- FastAPI framework for high-performance API development
- Python 3.13 with modern async support
- Docker containerization for easy deployment
- Health check endpoint for monitoring
- Lightweight container image

## Docker Instructions

### Building the Image

To build the Docker image with the name `ptronico-rinha25-api`:

```bash
docker build -t ptronico-rinha25-api .
```

### Running the Container

#### Basic Run
```bash
docker run -p 8000:8000 ptronico-rinha25-api
```

### Using with Docker Compose

Create a `docker-compose.yml` file in your project:

```yaml
version: '3.8'
services:
  api:
    image: ptronico-rinha25-api
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/"]
      interval: 30s
      timeout: 10s
      retries: 3
    restart: unless-stopped
```

Then run:
```bash
docker-compose up -d
```
