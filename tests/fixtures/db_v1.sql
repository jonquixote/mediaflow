-- Historical Pass A/B database: schema_version 1, no semantic markers.
-- Loaded by tests/test_migrations.py through Store.initialize().
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta(key,value) VALUES('schema_version','1');
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, preset TEXT NOT NULL, source_rel TEXT NOT NULL,
 source_path TEXT NOT NULL, original_path TEXT, parked_path TEXT,
 trash_path TEXT, manifest_path TEXT, entry_kind TEXT, state TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, first_seen REAL NOT NULL,
 plan_version INTEGER NOT NULL DEFAULT 0, plan_json TEXT,
 dry_run INTEGER NOT NULL DEFAULT 0, restarts INTEGER NOT NULL DEFAULT 0,
 review_reason TEXT, review_from_state TEXT, review_context TEXT, error TEXT,
 source_size INTEGER, source_mtime REAL, source_video_bytes INTEGER,
 source_duration REAL, output_path TEXT, output_sha256 TEXT, output_size INTEGER,
 output_video_bytes INTEGER, final_name TEXT, phase TEXT, progress REAL,
 encode_seconds REAL, backend TEXT, started_at REAL, finished_at REAL,
 fail_count INTEGER NOT NULL DEFAULT 0, last_error_at REAL,
 adopted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
 ts REAL NOT NULL, state TEXT, kind TEXT NOT NULL, message TEXT);
CREATE TABLE stats (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
 preset TEXT, backend TEXT, finished_at REAL NOT NULL, bytes_before INTEGER,
 bytes_after INTEGER, video_bytes_before INTEGER, video_bytes_after INTEGER,
 encode_seconds REAL);
CREATE TABLE speed_table (preset TEXT NOT NULL, backend TEXT NOT NULL,
 bucket TEXT NOT NULL, factor REAL NOT NULL, samples INTEGER NOT NULL DEFAULT 1,
 updated_at REAL NOT NULL, PRIMARY KEY(preset, backend, bucket));
INSERT INTO speed_table(preset,backend,bucket,factor,samples,updated_at)
 VALUES('legacy','qsv','>1080',2.0,1,1.0);
CREATE TABLE tmdb_cache (cache_key TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE library_cache (path TEXT PRIMARY KEY, size INTEGER NOT NULL,
 mtime REAL NOT NULL, probe TEXT, updated_at REAL NOT NULL);
CREATE TABLE commands (id TEXT PRIMARY KEY, command TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT);
CREATE TABLE intents (id TEXT PRIMARY KEY, kind TEXT NOT NULL, job_id TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE notifications (event_key TEXT NOT NULL, job_id TEXT,
 sent_at REAL NOT NULL, PRIMARY KEY(event_key, job_id));
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO jobs(id,preset,source_rel,source_path,original_path,entry_kind,state,
 created_at,updated_at,first_seen,plan_json,dry_run)
 VALUES('LEGACYJOB000000000000000001','legacy','Movie.mkv','/tmp/movie.mkv',
 '/tmp/movie.mkv','file','ENCODED',1.0,1.0,1.0,
 '{"schema":1,"video":{"color":{"range":"tv"}},"audio":[]}',0);
INSERT INTO intents(id,kind,job_id,payload,status,created_at,updated_at)
 VALUES('legacy-intent','replace','LEGACYJOB000000000000000001','{}','pending',1.0,1.0);
