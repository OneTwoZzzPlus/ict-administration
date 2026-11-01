#!/bin/bash
set -e

PRIMARY_HOST="${PRIMARY_HOST:-pg-primary}"
STANDBY_NAME="${STANDBY_NAME:-standby}"
PGDATA=/var/lib/postgresql/18/docker

until pg_isready -h "$PRIMARY_HOST" -p 5432 -U replicator; do
  echo ">>> primary не готов, жду 2 с"
  sleep 2
done

mkdir -p "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo ">>> данных нет, снимаю base backup"
  find "$PGDATA" -mindepth 1 -delete
  PGPASSWORD=replicator_password pg_basebackup \
    -h "$PRIMARY_HOST" -U replicator -D "$PGDATA" -P -Xs -R
else
  echo ">>> данные уже есть, пропускаю base backup"
fi

cp /etc/replica/postgresql.conf "$PGDATA/postgresql.conf"
cp /etc/replica/pg_hba.conf "$PGDATA/pg_hba.conf"

exec docker-entrypoint.sh postgres \
  -c "primary_conninfo=host=${PRIMARY_HOST} port=5432 user=replicator application_name=${STANDBY_NAME}"