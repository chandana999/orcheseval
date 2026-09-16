-- ---------------------------------------------------------------------------
-- eval-platform database setup. Run once as a PostgreSQL SUPERUSER.
--
--   psql -U postgres -h localhost -f scripts/setup_databases.sql
--
-- Creates the dedicated eval_platform and eval_platform_test databases and
-- grants the application role the privileges it needs. The application itself
-- never needs superuser or CREATEDB rights after this script has been run.
--
-- Safe to re-run: existing databases and roles are skipped, and only the
-- required privileges are re-verified/re-granted. Nothing is dropped and the
-- existing evalforge_local / evalforge_local_test databases are not touched.
--
-- To use different names, edit the three \set lines below.
-- ---------------------------------------------------------------------------

\set app_role      evalforge
\set app_db        eval_platform
\set app_test_db   eval_platform_test

-- The application role password is only applied when the role is created.
\set app_password  'evalforge'

\echo ''
\echo '== eval-platform database setup =='

-- 1. Application login role -------------------------------------------------
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L',
    :'app_role',
    :'app_password'
) AS stmt
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec

-- 2. Databases (owned by the application role, so migrations need no DDL grants)
SELECT format('CREATE DATABASE %I OWNER %I', :'app_db', :'app_role') AS stmt
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_db')
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'app_test_db', :'app_role') AS stmt
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_test_db')
\gexec

-- 3. Database-level privileges (idempotent) ---------------------------------
SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'app_db', :'app_role') AS stmt
\gexec
SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'app_test_db', :'app_role') AS stmt
\gexec

-- 4. Schema ownership inside each database ----------------------------------
\connect :app_db
SELECT format('ALTER SCHEMA public OWNER TO %I', :'app_role') AS stmt
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'app_role') AS stmt
\gexec

\connect :app_test_db
SELECT format('ALTER SCHEMA public OWNER TO %I', :'app_role') AS stmt
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'app_role') AS stmt
\gexec

-- 5. Verification -----------------------------------------------------------
\connect postgres
\echo ''
\echo '-- databases --'
SELECT d.datname, pg_get_userbyid(d.datdba) AS owner
FROM pg_database d
WHERE d.datname IN (:'app_db', :'app_test_db')
ORDER BY d.datname;

\echo '-- role privileges (expect has_create = t for both databases) --'
SELECT :'app_role' AS role,
       has_database_privilege(:'app_role', :'app_db', 'CREATE')      AS app_db_create,
       has_database_privilege(:'app_role', :'app_db', 'CONNECT')     AS app_db_connect,
       has_database_privilege(:'app_role', :'app_test_db', 'CREATE') AS test_db_create,
       has_database_privilege(:'app_role', :'app_test_db', 'CONNECT') AS test_db_connect;

\echo ''
\echo 'Setup complete. The application role needs no superuser rights from here on.'
\echo 'Next: python scripts/migrate.py upgrade'
\echo ''
