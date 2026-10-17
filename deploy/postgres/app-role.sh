#!/bin/sh
# Роль приложения для api и worker (REVIEW.md: D-3, дополнение ADR-0040): только DML на схему
# public, без DDL и прав суперпользователя; таблицы создаёт migrate владельцем POSTGRES_USER.
# Идемпотентно: при первом запуске тома — из docker-entrypoint-initdb.d, для уже созданного
# тома и смены APP_DB_PASSWORD — make deploy-db-roles. Пароль psql читает из окружения
# (\getenv, не argv), в SQL — литералом с экранированием (%L).
set -eu

APP_DB_USER="${APP_DB_USER:-tessera_app}"
if [ "$APP_DB_USER" = "$POSTGRES_USER" ]; then
	echo "app-role.sh: APP_DB_USER должен отличаться от POSTGRES_USER (владельца схемы)" >&2
	exit 1
fi
export APP_DB_PASSWORD

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
	-v app_user="$APP_DB_USER" -v owner="$POSTGRES_USER" -v db="$POSTGRES_DB" <<'SQL'
\getenv app_password APP_DB_PASSWORD
SELECT format('CREATE ROLE %I LOGIN', :'app_user')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L',
              :'app_user', :'app_password') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db', :'app_user') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user') \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
              :'app_user') \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', :'app_user') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', :'owner', :'app_user') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
              'GRANT USAGE, SELECT ON SEQUENCES TO %I', :'owner', :'app_user') \gexec
SQL
