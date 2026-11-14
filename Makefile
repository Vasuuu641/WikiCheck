DC = docker compose
SQL = $(DC) exec -T db mariadb -uwikirag -pwikirag wikirag

up:
	$(DC) up -d --wait

down:
	$(DC) down

# Apply every db/*.sql in order. Files are numbered; add new ones, never edit applied ones.
db:
	for f in $$(ls db/[0-9]*.sql | sort); do echo "applying $$f"; $(SQL) < $$f || exit 1; done

spike:
	$(SQL) -vvv < db/spike_vector_versioning.sql

api-install:
	python3 -m venv .venv && .venv/bin/pip install -r api/requirements.txt

api:
	.venv/bin/uvicorn api.main:app --port 8020

.PHONY: up down db spike api-install api
