.PHONY: help install test run-backend run-frontend docker-up docker-down clean

help:
	@echo ""
	@echo "  SDLC OSS Platform — Commands"
	@echo "  ─────────────────────────────"
	@echo "  make install       Install Python deps"
	@echo "  make setup         Create .env from example"
	@echo "  make test          Run 34 unit tests"
	@echo "  make run-backend   Start FastAPI server (port 8000)"
	@echo "  make run-frontend  Serve frontend (port 5173)"
	@echo "  make run-all       Start both backend and frontend"
	@echo "  make docker-up     Launch full stack via Docker Compose"
	@echo "  make docker-down   Stop Docker stack"
	@echo "  make clean         Remove cache files"
	@echo ""

install:
	pip install -r backend/requirements.txt

setup:
	@if [ ! -f backend/.env ]; then \
		cp backend/.env.example backend/.env; \
		echo "Created backend/.env — fill in GROQ_API_KEY"; \
	else echo "backend/.env already exists"; fi

test:
	cd backend && GROQ_API_KEY=$${GROQ_API_KEY:-test} python3 -m pytest tests/ -v

run-backend:
	cd backend && python3 main.py

run-frontend:
	cd frontend && python3 -m http.server 5173

run-all:
	@echo "Starting backend on :8000 and frontend on :5173"
	cd backend && python3 main.py &
	cd frontend && python3 -m http.server 5173

docker-up:
	docker compose up -d
	@echo ""
	@echo "  Frontend  →  http://localhost:5173"
	@echo "  API       →  http://localhost:8000"
	@echo "  API Docs  →  http://localhost:8000/docs"
	@echo "  MinIO UI  →  http://localhost:9001"
	@echo "  Gitea     →  http://localhost:3000"
	@echo "  Outline   →  http://localhost:3001"
	@echo "  Mattermost→  http://localhost:8065"

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	@echo "Cleaned."
