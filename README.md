# VerifiedX Spyglass Service

## Overview

This is the source code for the VFX Explorer service. It is built in Python and Django with a postgreSQL database.

To run your own you will need a wallet running (can be local or in the cloud). It will need to be launched like so:

```
./ReserveBlockCore enableapi openapi
```
Note: openapi is only required if you are running it remote from the service.

Then, update your env variable "RBX_WALLET_ADDRESS" to be `http://{server_ip OR localhost}:7292`

## Development

#### Dependencies

- [ ] Python 3.10.x
- [ ] Postgres 13.x
- [ ] Redis 6.x
- [ ] RabbitMQ 3.x

#### Setup

- [ ] [Create Environment](#create-environment)
- [ ] [Docker Services (Optional)](#docker-services-optional)
- [ ] [Configure Environment](#configure-environment)
- [ ] [Update Database](#update-database)
- [ ] [Run Application](#run-application)

#### Create Environment

Create Python virtual environment and install dependencies

```
python -m venv venv
source venv/bin/activate
make install
```

#### Docker Services (Optional)

Optionally you can run necessary services with Docker; db (postgres), cache (redis), and broker (rabbitmq).

```
docker compose -p rbx-explorer -f develop/docker-compose.yml up -d 
```

*WARNING*: The provided Docker compose configuration is intended for local development only.

```
DATABASE_URL=postgres://postgres:postgres@localhost:5432/postgres
REDIS_URL=redis://localhost:6379
CLOUDAMQP_URL=amqp://rabbitmq:rabbitmq@localhost:5672
```

With the services running you can view the RabbitMQ dashboard @ [http://localhost:15672](http://localhost:15672).

#### Configure Environment

Create `.env` file in the project root and configure.

```
cp .env.template .env
```

#### Update Database

Sync database with project.

```
python manage.py migrate
python manage.py loaddata initial_data
```

#### Run Application

Run development server

```
source venv/bin/activate
make run
```

Run web server

```
source venv/bin/activate
make web
```

Run worker

```
source venv/bin/activate
make worker
```


#### Syncing Blocks
To import blocks you will want to run the following command:

```
python manage.py sync_blocks
```

You'll likely want to run this in a cron job to keep things in sync.