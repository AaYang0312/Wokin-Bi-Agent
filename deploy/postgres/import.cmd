@echo off
setlocal enabledelayedexpansion
rem =====================================================================
rem  Restore pg_dump artifacts into the bi-agent-postgres container.
rem  One-time migration / disaster recovery. ASCII-only on purpose:
rem  cmd.exe reads .cmd as GBK, so Chinese comments would be mojibake.
rem
rem  usage:  import.cmd [dump-dir] [skipglobals]
rem  default dump-dir: D:\Projects\pg17\migration-20260912
rem  required files:   globals.sql  bi_agent.dump  bi_agent_test.dump
rem
rem  Roles come from globals.sql and only matter on an empty cluster.
rem  Objects are restored with --clean --if-exists, so re-running is safe.
rem  NOTE: since PG15, pg_dumpall does not export password hashes, so the
rem  bi_* roles arrive without passwords. That is fine while pg_hba is
rem  trust (all local DSNs are passwordless); run \password by hand if you
rem  ever switch to scram-sha-256.
rem =====================================================================
set "SRC=%~1"
if "%SRC%"=="" set "SRC=D:\Projects\pg17\migration-20260912"
set "CTN=bi-agent-postgres"

for %%F in (globals.sql bi_agent.dump bi_agent_test.dump) do if not exist "%SRC%\%%F" (
  echo [missing] %SRC%\%%F
  exit /b 2
)

docker inspect %CTN% >nul 2>&1
if errorlevel 1 (
  echo [error] container %CTN% not found. Run: docker compose up -d
  exit /b 3
)

if /I "%~2"=="skipglobals" goto skip_globals
echo == roles / grants ^(globals.sql^) ==
docker cp "%SRC%\globals.sql" "%CTN%:/tmp/globals.sql" || exit /b 4
rem ON_ERROR_STOP stays off on purpose: pg_dumpall also emits CREATE ROLE
rem postgres, which always fails on an already initialized cluster. The
rem role check below is what actually decides success.
docker exec %CTN% psql -U postgres -f /tmp/globals.sql
docker exec %CTN% psql -U postgres -Atc "select case when count(*) = 3 then 'roles OK' else 'roles MISSING: ' || count(*) end from pg_roles where rolname in ('bi_app','bi_reader','bi_sync')" | findstr /C:"roles OK" >nul
if errorlevel 1 (
  echo [error] required roles not created, see output above
  exit /b 5
)
:skip_globals

for %%D in (bi_agent bi_agent_test) do (
  echo == database %%D ==
  docker exec %CTN% psql -U postgres -Atc "select 1 from pg_database where datname='%%D'" | findstr /X /C:"1" >nul
  if errorlevel 1 (
    docker exec %CTN% psql -U postgres -v ON_ERROR_STOP=1 -c "create database %%D with template template0 locale_provider libc locale 'C' encoding 'UTF8' owner postgres" || exit /b 6
  )
  docker cp "%SRC%\%%D.dump" "%CTN%:/tmp/%%D.dump" || exit /b 7
  docker exec %CTN% pg_restore -U postgres -d %%D --clean --if-exists --exit-on-error /tmp/%%D.dump || exit /b 8
  docker exec %CTN% psql -U postgres -d %%D -q -c "vacuum analyze" || exit /b 9
  docker exec %CTN% psql -U postgres -d %%D -Atc "select 'tables='||count(*) from pg_tables where schemaname in ('bi','reporting')"
  docker exec %CTN% psql -U postgres -d %%D -Atc "select 'views='||count(*) from pg_views where schemaname in ('bi','reporting')"
)

echo == database attributes ^(must be UTF8 / C / libc, same as before^) ==
docker exec %CTN% psql -U postgres -d bi_agent_test -Atc "select datname, pg_encoding_to_char(encoding), datcollate, datctype, datlocprovider from pg_database order by 1"
docker exec %CTN% psql -U postgres -d bi_agent_test -Atc "select name, setting from pg_settings where name in ('TimeZone','log_timezone','listen_addresses','server_version')"
echo done
