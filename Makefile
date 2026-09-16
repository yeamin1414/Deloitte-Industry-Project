.PHONY: install run test lint format simulate docker emulators tf-check

install:
	python -m pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --port 8000

test:
	pytest --cov --cov-report=term-missing

lint:
	ruff check . && ruff format --check .

format:
	ruff format . && ruff check . --fix

simulate:
	python -m simulator.device_simulator --url http://localhost:8000 --count 200 --rate 20

docker:
	docker compose up --build

emulators:
	docker compose --profile emulators up --build

tf-check:
	terraform -chdir=infra/terraform/aws init -backend=false && terraform -chdir=infra/terraform/aws validate
	terraform -chdir=infra/terraform/azure init -backend=false && terraform -chdir=infra/terraform/azure validate
