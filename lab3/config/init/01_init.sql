-- Dedicated replication user — replication privileges only
CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD 'replicator_password';

-- Application user
CREATE USER app WITH ENCRYPTED PASSWORD 'app_password';

-- Application database
CREATE DATABASE runtime_sentinel OWNER app;

GRANT CONNECT ON DATABASE runtime_sentinel TO replicator;