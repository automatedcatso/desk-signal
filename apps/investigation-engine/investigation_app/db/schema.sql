-- Investigation Intelligence Engine - SQLite schema (WAL + FTS5).
-- Every statement is idempotent (IF NOT EXISTS) so init_db() is safe to call
-- on every startup. One investigation ("case") owns everything.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Core investigation ------------------------------------------------------
CREATE TABLE IF NOT EXISTS cases (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  uid          TEXT UNIQUE NOT NULL,
  title        TEXT NOT NULL,
  reference_no     TEXT,
  status       TEXT NOT NULL DEFAULT 'open',      -- open | archived | closed
  ai_mode      TEXT NOT NULL DEFAULT 'standard',  -- standard | smart | deep
  metadata_json TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- Participants / related_parties / reviewers ------------------------------------------
CREATE TABLE IF NOT EXISTS people (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id  INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  role     TEXT NOT NULL,                          -- participant | related_party | reviewer
  name     TEXT,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_people_case ON people(case_id);

-- Evidence (original files copied read-only, deduped by sha256) -----------
CREATE TABLE IF NOT EXISTS evidence (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id       INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  original_name TEXT,
  stored_path   TEXT,
  mime          TEXT,
  size          INTEGER,
  sha256        TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending|processing|done|error
  progress_percent REAL NOT NULL DEFAULT 0,
  progress_current INTEGER DEFAULT 0,
  progress_total INTEGER DEFAULT 0,
  progress_detail TEXT,
  meta_json     TEXT,
  intel_json    TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(case_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id);

-- Entity engine (store once, cross-reference automatically) ---------------
CREATE TABLE IF NOT EXISTS entities (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id  INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  type     TEXT NOT NULL,      -- phone|email|imei|imsi|iccid|upi|ip|url|domain|gps|wallet|vehicle|account|ifsc|name|org|date
  value    TEXT NOT NULL,
  norm     TEXT NOT NULL,
  UNIQUE(case_id, type, norm)
);
CREATE INDEX IF NOT EXISTS idx_entities_case ON entities(case_id);

CREATE TABLE IF NOT EXISTS entity_links (
  entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  PRIMARY KEY (entity_id, evidence_id)
);


-- Structured financial transaction intelligence -------------------------
CREATE TABLE IF NOT EXISTS transactions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id           INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  evidence_id       INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  source_file       TEXT,
  source_ref        TEXT,
  file_hash         TEXT,
  layer             INTEGER,
  txn_date          TEXT,
  utr               TEXT,
  amount            REAL,
  disputed_amount   REAL,
  lien_amount       REAL,
  sender_account    TEXT,
  receiver_account  TEXT,
  account_no        TEXT,
  ifsc              TEXT,
  bank              TEXT,
  upi               TEXT,
  wallet            TEXT,
  merchant          TEXT,
  status            TEXT,
  remarks           TEXT,
  meta_json         TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_case ON transactions(case_id);
CREATE INDEX IF NOT EXISTS idx_transactions_evidence ON transactions(evidence_id);
CREATE INDEX IF NOT EXISTS idx_transactions_utr ON transactions(case_id, utr);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(case_id, account_no);
CREATE INDEX IF NOT EXISTS idx_transactions_ifsc ON transactions(case_id, ifsc);



-- Universal social/chat/technical intelligence ---------------------------
CREATE TABLE IF NOT EXISTS communications (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id           INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  evidence_id       INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  platform          TEXT,
  sender            TEXT,
  receiver          TEXT,
  sender_handle     TEXT,
  receiver_handle   TEXT,
  message_text      TEXT,
  timestamp         TEXT,
  entities_json     TEXT,
  attachments_json  TEXT,
  urls_json         TEXT,
  amounts_json      TEXT,
  risk_flags_json   TEXT,
  source_ref        TEXT,
  confidence        REAL,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_communications_case ON communications(case_id);
CREATE INDEX IF NOT EXISTS idx_communications_evidence ON communications(evidence_id);
CREATE INDEX IF NOT EXISTS idx_communications_platform ON communications(case_id, platform);

CREATE TABLE IF NOT EXISTS social_profiles (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  evidence_id     INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  platform        TEXT,
  profile_name    TEXT,
  username        TEXT,
  profile_url     TEXT,
  bio             TEXT,
  metadata_json   TEXT,
  source_ref      TEXT,
  confidence      REAL,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_social_profiles_case ON social_profiles(case_id);
CREATE INDEX IF NOT EXISTS idx_social_profiles_username ON social_profiles(case_id, username);

CREATE TABLE IF NOT EXISTS technical_indicators (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  evidence_id     INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  type            TEXT NOT NULL,
  value           TEXT NOT NULL,
  norm            TEXT NOT NULL,
  source_ref      TEXT,
  confidence      REAL,
  metadata_json   TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE(case_id, evidence_id, type, norm)
);
CREATE INDEX IF NOT EXISTS idx_technical_indicators_case ON technical_indicators(case_id);
CREATE INDEX IF NOT EXISTS idx_technical_indicators_norm ON technical_indicators(case_id, type, norm);

-- Timeline (auto-merged chronological events) ----------------------------
CREATE TABLE IF NOT EXISTS timeline (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id            INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  ts                 TEXT,
  source_evidence_id INTEGER REFERENCES evidence(id) ON DELETE SET NULL,
  kind               TEXT,
  summary            TEXT
);
CREATE INDEX IF NOT EXISTS idx_timeline_case ON timeline(case_id, ts);

-- Chain of custody / integrity ------------------------------------------
CREATE TABLE IF NOT EXISTS chain_of_custody (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  action      TEXT NOT NULL,
  sha256      TEXT,
  actor       TEXT,
  at          TEXT NOT NULL
);

-- Investigator workspace artefacts ---------------------------------------
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  body TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  ref_type TEXT, ref_id INTEGER, label TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  title TEXT, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_chats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  role TEXT, content TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aichats_case ON ai_chats(case_id);

-- Embedding cache: reuse by sha256 so unchanged evidence is never reprocessed.
-- One row per AI chunk. ``text`` holds the chunk so retrieval can return it
-- without re-reading the original evidence file (offline, fast).
CREATE TABLE IF NOT EXISTS embeddings (
  chunk_id    TEXT PRIMARY KEY,
  evidence_id INTEGER REFERENCES evidence(id) ON DELETE CASCADE,
  case_id     INTEGER REFERENCES cases(id) ON DELETE CASCADE,
  sha256      TEXT,
  seq         INTEGER,
  text        TEXT,
  vec         BLOB
);
CREATE INDEX IF NOT EXISTS idx_embeddings_case ON embeddings(case_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_evidence ON embeddings(evidence_id);

-- Per-stage processing status so one failing extractor never hides the rest
-- and the UI/AI can see exactly how far an item progressed.
CREATE TABLE IF NOT EXISTS evidence_stages (
  evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  stage       TEXT NOT NULL,
  state       TEXT NOT NULL,           -- ok | error | skipped
  detail      TEXT,
  at          TEXT NOT NULL,
  PRIMARY KEY (evidence_id, stage)
);

-- Knowledge-graph edges: two entities that co-occur in the same evidence.
-- Store-once semantics keep the graph incremental (weight counts evidences).
CREATE TABLE IF NOT EXISTS relationships (
  case_id  INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  src_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  weight   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (case_id, src_id, dst_id)
);
CREATE INDEX IF NOT EXISTS idx_relationships_case ON relationships(case_id);

-- Near-duplicate / similarity edges between evidence items (content-based).
CREATE TABLE IF NOT EXISTS evidence_similarity (
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  a_id    INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  b_id    INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  score   REAL NOT NULL,
  kind    TEXT NOT NULL,               -- near_duplicate | similar | shared_entities | financial_link
  reasons TEXT,
  PRIMARY KEY (case_id, a_id, b_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_similarity_case ON evidence_similarity(case_id);

CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
  msg TEXT, at TEXT NOT NULL
);




-- Session / crash recovery / layout (autosaved) --------------------------
CREATE TABLE IF NOT EXISTS workspace (
  case_id    INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
  state_json TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Global full-text search across all textual artefacts -------------------
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
  case_id UNINDEXED,
  ref_type UNINDEXED,
  ref_id UNINDEXED,
  content
);

-- Processing speed indexes ------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_entity_links_evidence ON entity_links(evidence_id);
CREATE INDEX IF NOT EXISTS idx_entity_links_entity ON entity_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_entities_case_type_norm ON entities(case_id, type, norm);
CREATE INDEX IF NOT EXISTS idx_transactions_sender ON transactions(case_id, sender_account);
CREATE INDEX IF NOT EXISTS idx_transactions_receiver ON transactions(case_id, receiver_account);
CREATE INDEX IF NOT EXISTS idx_transactions_upi ON transactions(case_id, upi);
CREATE INDEX IF NOT EXISTS idx_transactions_bank ON transactions(case_id, bank);
CREATE INDEX IF NOT EXISTS idx_transactions_amount ON transactions(case_id, amount);
