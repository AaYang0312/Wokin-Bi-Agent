@echo off
setlocal
rem =====================================================================
rem  Logical backup: container -> deploy\postgres\backup\<timestamp>
rem  (that directory is Git-ignored). Output layout matches import.cmd,
rem  so a backup can restore onto a fresh host or a new container.
rem
rem  globals.sql carries roles/grants only -- since PG15 it no longer
rem  contains password hashes, so roles arrive passwordless on restore.
rem  Under trust auth (see pg_hba.conf) that is intentional; if you ever
rem  switch to scram-sha-256, re-set the role passwords by hand.
rem
rem  usage:  backup.cmd
rem =====================================================================
set "CTN=bi-agent-postgres"
set "DST=%~dp0backup"
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "TS=%%T"
set "DST=%DST%\%TS%"

docker inspect %CTN% >nul 2>&1
if errorlevel 1 (
  echo [error] container %CTN% not found
  exit /b 3
)

mkdir "%DST%" || exit /b 1
echo == dump inside container ==
docker exec %CTN% pg_dumpall -U postgres --globals-only -f /tmp/globals.sql || exit /b 1
docker exec %CTN% pg_dump -U postgres -Fc -d bi_agent -f /tmp/bi_agent.dump || exit /b 1
docker exec %CTN% pg_dump -U postgres -Fc -d bi_agent_test -f /tmp/bi_agent_test.dump || exit /b 1

echo == copy out ==
for %%F in (globals.sql bi_agent.dump bi_agent_test.dump) do docker cp "%CTN%:/tmp/%%F" "%DST%\%%F" || exit /b 1

rem SHA256.txt is this command's redirect target, so it must be filtered out
rem before hashing: Get-FileHash reads every path it is handed, and cmd.exe has
rem already created (and holds open) the output file. -Exclude is ignored with
rem -LiteralPath, hence the explicit Where-Object.
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%DST%' -File | Where-Object { $_.Name -ne 'SHA256.txt' } | Get-FileHash -Algorithm SHA256 | ForEach-Object { $_.Hash + '  ' + (Split-Path $_.Path -Leaf) }" > "%DST%\SHA256.txt"
docker exec %CTN% rm -f /tmp/globals.sql /tmp/bi_agent.dump /tmp/bi_agent_test.dump

echo == checksums ==
type "%DST%\SHA256.txt"
echo backup written to %DST%
