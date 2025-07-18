COMPOSE_PROJECT_NAME = vfx-explorer

# ---- Docker Helpers ----
build:
	docker compose build

up:
	docker compose -p $(COMPOSE_PROJECT_NAME) up --detach

down:
	docker compose -p $(COMPOSE_PROJECT_NAME) down

restart:
	docker compose -p $(COMPOSE_PROJECT_NAME) down && docker compose up --detach

bash:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web bash

wipe:
	@read -p "⚠️  This will stop and remove all containers and volumes. Continue? (y/N) " CONFIRM; \
	if [ "$$CONFIRM" = "y" ] || [ "$$CONFIRM" = "Y" ]; then \
		docker compose -p $(COMPOSE_PROJECT_NAME) down -v; \
	else \
		echo "❌ Cancelled."; \
	fi
	

# ---- Django Management ----

manage:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py $(filter-out $@,$(MAKECMDGOALS))

%:
	@:

migrate:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py migrate

makemigrations:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py makemigrations

shell:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py shell

createsuperuser:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py createsuperuser

collectstatic:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py collectstatic --noinput

# ---- Dev Server (inside container) ----
dev:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py runserver 0.0.0.0:8000


# ---- Celery ----
celery:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web celery -A config worker --loglevel=info

# ---- Logs ----
logs:
	docker compose -p $(COMPOSE_PROJECT_NAME) logs -f web

logs-celery:
	docker compose -p $(COMPOSE_PROJECT_NAME) logs -f web | grep celery

# ---- Testing ----
test:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py test


# ---- Admin ----
admin_theme_dump:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py dumpdata admin_interface.Theme --indent 4 -o admin/fixtures/admin_interface_theme.json 

admin_theme_load:
	docker compose -p $(COMPOSE_PROJECT_NAME) exec web python manage.py loaddata ./admin/fixtures/admin_interface_theme.json


	


shell_main:
	porter app run rbx-explorer-mainnet -- bash

shell_testnet:
	porter app run rbx-explorer-testnet -e -- bash

logs_main_runner:
	porter app logs rbx-explorer-mainnet --service runner

logs_main_worker:
	porter app logs rbx-explorer-mainnet --service worker

run_cli:
	/Applications/VFXWallet.app/Contents/Resources/RBXCore/ReserveBlockCore enableapi


wal_testnet:
	ssh root@159.203.76.174

