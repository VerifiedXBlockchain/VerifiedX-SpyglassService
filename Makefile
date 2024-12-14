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
	heroku run bash -a rbx-explorer-service

shell_testnet:
	heroku run bash -a rbx-explorer-service-testnet

run_cli:
	/Applications/RBXWallet.app/Contents/Resources/RBXCore/ReserveBlockCore enableapi


wal_testnet:
	ssh root@164.92.105.169