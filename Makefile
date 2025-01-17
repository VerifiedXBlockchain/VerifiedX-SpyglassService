.PHONY: install run release web worker

install:
	pip install -r requirements.txt

run:
	python manage.py runserver 0.0.0.0:8000

release:
	python manage.py migrate

web:
	gunicorn project.wsgi --log-file -

worker:
	celery --app=project worker --without-heartbeat --without-gossip --without-mingle --loglevel=INFO

watch_worker:
	python manage.py watch_worker
	
deploy_main:
	heroku git:remote -a rbx-explorer-service && git push heroku main

deploy_testnet:
	heroku git:remote -a rbx-explorer-service-testnet && git push heroku testnet:main

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

