-- Peer-auth role for sidecar (zextras OS user → zextras DB role)
-- Run as: sudo -u postgres psql -d "carbonio-files-db" -f postgres_role.sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'zextras') THEN
    CREATE ROLE zextras WITH LOGIN;
  END IF;
END$$;
GRANT CONNECT ON DATABASE "carbonio-files-db" TO zextras;
GRANT USAGE ON SCHEMA public TO zextras;
GRANT SELECT ON public.link TO zextras;
GRANT SELECT ON public.node TO zextras;
GRANT SELECT ON public.revision TO zextras;
