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

---

```yaml
x-api: &api
  # image: ghcr.io/ptronico/rinha25:latest
  build:
    context: ./../../../rinha25
    target: dev
  environment:
    - APP_PORT=8000
    - REDIS_HOST=redis
    - REDIS_PORT=6379
    - PROCESSOR_DEFAULT_URL=http://payment-processor-default:8080
    - PROCESSOR_FALLBACK_URL=http://payment-processor-fallback:8080
  volumes:
    - backend_socket:/shared
  networks:
    - backend
    - payment-processor
  depends_on:
    - redis
  deploy:
    resources:
      limits:
        cpus: "0.65"
        memory: "60MB"
  # develop:
  #   watch:
  #     - action: sync
  #       path: ../../../rinha25
  #       target: /app
  #       ignore:
  #         - .venv/
  #     - action: rebuild
  #       path: ./uv.lock

services:
  nginx:
    image: nginx:1.25-alpine
    container_name: rinha-nginx
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - backend_socket:/shared
    depends_on:
      - api1
      - api2
    ports:
      - "9999:9999"
    networks:
      - backend
      - payment-processor
    # deploy:
    #   resources:
    #     limits:
    #       cpus: "0.5"
    #       memory: "20MB"
  api1:
    <<: *api
    hostname: api1
    command: uvicorn src:app --uds /shared/backend1.sock --no-access-log --workers 2 --loop uvloop --http httptools
  api2:
    <<: *api
    hostname: api2
    command: uvicorn src:app --uds /shared/backend2.sock --no-access-log --workers 2 --loop uvloop --http httptools
  redis:
    image: redis:7.2-alpine
    container_name: rinha-redis
    command: redis-server --save "" --appendonly no
    networks:
      - backend
    # deploy:
    #   resources:
    #     limits:
    #       cpus: "0.1"
    #       memory: "20MB"
networks:
  backend:
    driver: bridge
  payment-processor:
    external: true
volumes:
  backend_socket:
```

```config
worker_processes auto;
events {
    # worker_connections 4096;
    use epoll;
    # multi_accept on;
}

http {
    access_log off;
    error_log /dev/null crit;

    # sendfile on;
    # tcp_nopush on;
    # tcp_nodelay on;

    # keepalive_timeout 30;
    # keepalive_requests 100000;

    # client_max_body_size 0;

    upstream api {
        # server api1:8000; # max_fails=1 fail_timeout=1s;
        # server api2:8000; # max_fails=1 fail_timeout=1s;
        # server api3:8000 max_fails=1 fail_timeout=1s;
        # server api4:8000 max_fails=1 fail_timeout=1s;
        server unix:/shared/backend1.sock;
        server unix:/shared/backend2.sock;
        # keepalive 32;
    }

    server {
        listen 9999 default_server;

        location /payments-summary {
            proxy_pass http://api;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
        }

        location /payments {
            # This sends back 202 right away, no backend call
            add_header Content-Type application/json;
            return 202 '{"status": "accepted"}';
        }
    }
}
```
