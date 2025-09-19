# Makefile for Avassa ACME Cloudflare Callback

# Docker image configuration
IMAGE_NAME ?= avassa/acme-cloudflare-callback
IMAGE_TAG ?= latest
FULL_IMAGE_NAME = $(IMAGE_NAME):$(IMAGE_TAG)

# Docker registry (override with your registry)
REGISTRY ?= 

# Container name for running
CONTAINER_NAME ?= acme-callback-service

.PHONY: help build run stop clean push pull logs shell test lint

# Default target
help:
	@echo "Available targets:"
	@echo "  build     - Build Docker image"
	@echo "  run       - Run container with docker-compose"
	@echo "  stop      - Stop running container"
	@echo "  clean     - Remove Docker image and containers"
	@echo "  push      - Push image to registry"
	@echo "  pull      - Pull image from registry"
	@echo "  logs      - Show container logs"
	@echo "  shell     - Open shell in running container"
	@echo "  test      - Run tests (if any)"
	@echo "  lint      - Run Python linting"
	@echo ""
	@echo "Variables:"
	@echo "  IMAGE_NAME=$(IMAGE_NAME)"
	@echo "  IMAGE_TAG=$(IMAGE_TAG)"
	@echo "  REGISTRY=$(REGISTRY)"

# Build Docker image
build:
	@echo "Building Docker image: $(FULL_IMAGE_NAME)"
	docker build -t $(FULL_IMAGE_NAME) .
	@echo "Image built successfully!"

# Build with no cache
build-no-cache:
	@echo "Building Docker image with no cache: $(FULL_IMAGE_NAME)"
	docker build --no-cache -t $(FULL_IMAGE_NAME) .
	@echo "Image built successfully!"

# Tag image with registry
tag:
ifdef REGISTRY
	docker tag $(FULL_IMAGE_NAME) $(REGISTRY)/$(FULL_IMAGE_NAME)
	@echo "Tagged image: $(REGISTRY)/$(FULL_IMAGE_NAME)"
else
	@echo "Error: REGISTRY variable not set"
	@exit 1
endif

# Push to registry
push: tag
ifdef REGISTRY
	@echo "Pushing image to registry: $(REGISTRY)/$(FULL_IMAGE_NAME)"
	docker push $(REGISTRY)/$(FULL_IMAGE_NAME)
	@echo "Image pushed successfully!"
else
	@echo "Error: REGISTRY variable not set"
	@exit 1
endif

# Pull from registry
pull:
ifdef REGISTRY
	@echo "Pulling image from registry: $(REGISTRY)/$(FULL_IMAGE_NAME)"
	docker pull $(REGISTRY)/$(FULL_IMAGE_NAME)
	docker tag $(REGISTRY)/$(FULL_IMAGE_NAME) $(FULL_IMAGE_NAME)
else
	@echo "Error: REGISTRY variable not set"
	@exit 1
endif

# Run with docker-compose
run:
	@echo "Starting service with docker-compose..."
	@if [ ! -f .env ]; then \
		echo "Warning: .env file not found. Copy .env.template to .env and configure it."; \
		echo "cp .env.template .env"; \
		exit 1; \
	fi
	docker-compose up -d
	@echo "Service started! Use 'make logs' to see output."

# Run in foreground
run-fg:
	@echo "Starting service with docker in foreground..."
	@if [ ! -f .env ]; then \
		echo "Warning: .env file not found. Copy .env.template to .env and configure it."; \
		echo "cp .env.template .env"; \
		exit 1; \
	fi
	docker run --rm -it --env-file .env $(FULL_IMAGE_NAME)

# Stop services
stop:
	@echo "Stopping services..."
	docker-compose down
	@echo "Services stopped."

# Stop and remove volumes
stop-clean:
	@echo "Stopping services and removing volumes..."
	docker-compose down -v
	@echo "Services stopped and volumes removed."

# Show logs
logs:
	docker-compose logs -f

# Show logs for specific service
logs-service:
	docker-compose logs -f acme-callback

# Open shell in running container
shell:
	@echo "Opening shell in running container..."
	@if docker-compose ps | grep -q "acme-callback.*Up"; then \
		docker-compose exec acme-callback /bin/bash; \
	else \
		echo "Container not running. Starting temporary container..."; \
		docker run -it --rm $(FULL_IMAGE_NAME) /bin/bash; \
	fi

# Run Python script directly (for development)
run-dev:
	@echo "Running Python script directly..."
	@if [ ! -f .env ]; then \
		echo "Warning: .env file not found. Copy .env.example to .env and configure it."; \
		exit 1; \
	fi
	@export $$(cat .env | grep -v '^#' | xargs) && python3 acme_callback.py

# Install Python dependencies locally
install-deps:
	@echo "Installing Python dependencies..."
	pip3 install -r requirements.txt

# Run Python linting
lint:
	@echo "Running Python linting..."
	@if command -v flake8 >/dev/null 2>&1; then \
		flake8 acme_callback.py; \
	else \
		echo "flake8 not installed. Install with: pip install flake8"; \
	fi
	@if command -v black >/dev/null 2>&1; then \
		black --check acme_callback.py; \
	else \
		echo "black not installed. Install with: pip install black"; \
	fi

# Format Python code
format:
	@echo "Formatting Python code..."
	@if command -v black >/dev/null 2>&1; then \
		black acme_callback.py; \
	else \
		echo "black not installed. Install with: pip install black"; \
	fi

# Run tests (placeholder for when tests are added)
test:
	@echo "Running tests..."
	@if [ -f test_acme_callback.py ]; then \
		python3 -m pytest test_acme_callback.py -v; \
	else \
		echo "No tests found. Create test_acme_callback.py to run tests."; \
	fi

# Clean up Docker artifacts
clean:
	@echo "Cleaning up Docker artifacts..."
	-docker-compose down -v 2>/dev/null
	-docker rmi $(FULL_IMAGE_NAME) 2>/dev/null
	-docker system prune -f
	@echo "Cleanup completed."

# Clean up everything (including registry images)
clean-all: clean
ifdef REGISTRY
	-docker rmi $(REGISTRY)/$(FULL_IMAGE_NAME) 2>/dev/null
endif
	@echo "Full cleanup completed."

# Setup development environment
setup-dev:
	@echo "Setting up development environment..."
	@if [ ! -f .env ]; then \
		cp .env.template .env; \
		echo "Created .env file from template. Please edit it with your values."; \
	fi
	$(MAKE) install-deps
	@echo "Development environment setup complete!"

# Check if all required tools are available
check-tools:
	@echo "Checking required tools..."
	@command -v docker >/dev/null 2>&1 || (echo "Error: docker not found" && exit 1)
	@command -v docker-compose >/dev/null 2>&1 || (echo "Error: docker-compose not found" && exit 1)
	@command -v python3 >/dev/null 2>&1 || (echo "Error: python3 not found" && exit 1)
	@command -v pip3 >/dev/null 2>&1 || (echo "Error: pip3 not found" && exit 1)
	@echo "All required tools are available!"

# Show container status
status:
	@echo "Container status:"
	docker-compose ps

# Restart services
restart: stop run

# View container resource usage
stats:
	@echo "Container resource usage:"
	docker-compose ps -q | xargs docker stats --no-stream
