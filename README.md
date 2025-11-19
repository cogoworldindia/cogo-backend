🚀 Microservices Backend Infrastructure — Docker Setup

This repository contains a complete Dockerized backend infrastructure for a microservices-based application.
It includes:

Auth Service (FastAPI)

User Service (FastAPI)

Mail Service (Spring Boot)

API Gateway (FastAPI)

Optional fallback services:

PostgreSQL

Redis

All services run inside a shared Docker network and communicate internally using service names.

📁 Project Structure
.
├── docker-compose.yml
├── .env
└── services/
    ├── auth/
    ├── user/
    ├── mail/
    └── api-gateway/


Each service maintains its own .env file inside its folder.

🔧 Prerequisites

Before you run the setup:

Docker 20+

Docker Compose v2+

A valid .env file in the root directory

⚙️ Environment Variables

Your root .env file must contain:

POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
<!-- POSTGRES_DB=appdb -->


Each microservice will have its own local .env file inside the service folder for service-specific configs.

🐳 Docker Compose Overview

This setup defines:

Core Microservices
Service	Local URL	Docker Internal URL
Auth Service	http://localhost:8001
	http://auth-service:8000

User Service	http://localhost:8002
	http://user-service:8000

Mail Service	http://localhost:8080
	http://mail-service:8081

API Gateway	http://localhost:8000
	http://api-gateway:8000
Optional Fallback Services (Only run when required)
Service	Purpose
Redis	In-case system Redis is not available
Postgres	Local database fallback
▶️ How to Run the Microservices
Start All Core Services
docker compose up -d

Start With Fallback Services (Redis + Postgres)
docker compose --profile fallback up -d

Restart
docker compose down
docker compose up -d

🌐 How Services Communicate Internally

Inside Docker, each microservice calls others using service names, such as:

AUTH_SERVICE_URL=http://auth-service:8000
USER_SERVICE_URL=http://user-service:8000
MAIL_SERVICE_URL=http://mail-service:8081


For Redis (when fallback is enabled):

REDIS_HOST=redis
REDIS_PORT=6379


For external host Redis:

REDIS_HOST=host.docker.internal
REDIS_PORT=6379

🌍 Accessing Services from Browser
Service	URL
API Gateway	http://localhost:8000

Auth Service	http://localhost:8001

User Service	http://localhost:8002

Mail Service	http://localhost:8080
🧪 Health Check Endpoints

Each service exposes:

/health for FastAPI services

/actuator/health for Spring Boot mail-service

Example:

curl http://localhost:8001/health
curl http://localhost:8080/actuator/health

🛠️ Docker Compose File ( Explained )
Microservices

Each service:

Uses its own Dockerfile in /services/<service>/

Loads its environment variables from .env

Exposes ports for browser testing

Communicates over the backend network

Fallback Services

Disabled by default

Start only when you enable --profile fallback

🗂 Volumes
volumes:
  pgdata:  # stores Postgres data

❗ Common Issues & Fixes
1. Connection refused Redis

✔ Ensure system Redis is running:

sudo systemctl status redis


✔ Or run fallback Redis:

docker compose --profile fallback up -d


✔ Set REDIS_HOST=host.docker.internal

2. Microservice cannot reach database

Check your .env:

DATABASE_HOST=host.docker.internal

3. Mail Service not accessible

Your Spring Boot service runs on 8081 inside container.
Compose maps it to 8080 on host.

Access via:

http://localhost:8080
