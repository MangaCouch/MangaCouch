"""Complete SQLite schema for a MangaCouch library, grouped by domain."""

APPLICATION_ID = 0x4D434C31  # MCL1: the library format introduced in 1.0.0.
SCHEMA_VERSION = 2
SCHEMA = """
-- Books and content -------------------------------------------------------
CREATE TABLE book (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256) = 64),
    content_json TEXT NOT NULL CHECK(json_valid(content_json)),
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    search_text TEXT NOT NULL,
    language_key TEXT NOT NULL DEFAULT '',
    work_type_key TEXT NOT NULL DEFAULT '',
    recent_order INTEGER NOT NULL DEFAULT 0,
    indexed_pages INTEGER NOT NULL DEFAULT 0 CHECK(indexed_pages >= 0),
    metadata_revision INTEGER NOT NULL DEFAULT 1 CHECK(metadata_revision >= 1),
    review_state TEXT NOT NULL DEFAULT 'needs_review'
        CHECK(review_state IN ('needs_review', 'confirmed')),
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK(availability IN ('available','missing','changed','unreadable')),
    source_site_key TEXT NOT NULL DEFAULT '',
    source_rating REAL
);
CREATE INDEX book_pending_features ON book(id) WHERE availability='available'
    AND indexed_pages<json_extract(content_json,'$.page_count');
CREATE INDEX book_path ON book(json_extract(content_json,'$.relative_path'));
CREATE INDEX book_language ON book(language_key,id);
CREATE INDEX book_work_type ON book(work_type_key,id);
CREATE INDEX book_recent ON book(recent_order,id);
CREATE INDEX book_source_identity ON book(
    json_extract(metadata_json,'$.source.gallery_id'),
    json_extract(metadata_json,'$.source.site')
);
CREATE TABLE thumbnail_revision (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision >= 1)
);
CREATE TABLE archive_manifest (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    pages_json TEXT NOT NULL CHECK(json_valid(pages_json)),
    file_stamp TEXT NOT NULL CHECK(json_valid(file_stamp))
);
CREATE TABLE book_tag (
 book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
 namespace TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(book_id,namespace,value)
) WITHOUT ROWID;
CREATE INDEX book_tag_value ON book_tag(namespace,value,book_id);
CREATE TABLE tag_alias (
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    translation TEXT NOT NULL,
    search_text TEXT NOT NULL,
    PRIMARY KEY(namespace,value)
);
CREATE TABLE tag_translation (
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    translation TEXT NOT NULL,
    search_text TEXT NOT NULL,
    PRIMARY KEY(namespace,value)
);
CREATE VIEW effective_tag_alias AS SELECT * FROM tag_alias UNION ALL SELECT * FROM tag_translation t
WHERE NOT EXISTS (SELECT 1 FROM tag_alias a WHERE a.namespace=t.namespace AND a.value=t.value);
CREATE TABLE translation_update (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    updated_at INTEGER NOT NULL,
    revision TEXT NOT NULL
);
CREATE TABLE metadata_import (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    report_json TEXT NOT NULL CHECK(json_valid(report_json)),
    decisions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(decisions_json))
);
CREATE TABLE review_event (
    id INTEGER PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    metadata_revision INTEGER NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK(json_valid(details_json))
);
CREATE INDEX review_event_book ON review_event(book_id,id);
CREATE TABLE sidecar_sync (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    pending_revision INTEGER NOT NULL CHECK(pending_revision >= 1),
    synced_revision INTEGER NOT NULL DEFAULT 0 CHECK(synced_revision >= 0),
    baseline_hash TEXT,
    observed_hash TEXT,
    intent_hash TEXT,
    last_error TEXT,
    CHECK(synced_revision <= pending_revision)
);
CREATE TABLE sidecar_conflict (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
    state TEXT NOT NULL CHECK(state IN ('pending','resolved','superseded')),
    resolution_json TEXT
);
CREATE UNIQUE INDEX sidecar_pending_conflict ON sidecar_conflict(book_id) WHERE state='pending';
CREATE INDEX sidecar_conflict_book ON sidecar_conflict(book_id);
CREATE TABLE eze_cache (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    synced_hash TEXT
);
CREATE TABLE library_root (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    root_id TEXT NOT NULL UNIQUE
);
CREATE TABLE library_preferences (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    settings_json TEXT NOT NULL CHECK(json_valid(settings_json)),
    revision INTEGER NOT NULL CHECK(revision >= 1)
);

-- Reading and personal state ---------------------------------------------
CREATE TABLE personal (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)
);
CREATE TABLE reading_event (
    id INTEGER PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    page INTEGER,
    completed INTEGER NOT NULL CHECK(completed IN (0,1)),
    kind TEXT NOT NULL CHECK(kind IN ('progress','completed','reset')),
    is_reading INTEGER NOT NULL DEFAULT 1 CHECK(is_reading IN (0,1))
);
CREATE INDEX reading_event_time ON reading_event(created_at,id);
CREATE INDEX reading_event_book ON reading_event(book_id,id);
CREATE TABLE reading_activity (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    updated_at INTEGER NOT NULL
);
-- Negative timestamps make both recent-order keys ascending, including unread books at zero.
CREATE TRIGGER activity_order_insert AFTER INSERT ON reading_activity BEGIN
    UPDATE book SET recent_order=-new.updated_at WHERE id=new.book_id;
END;
CREATE TRIGGER activity_order_update AFTER UPDATE ON reading_activity BEGIN
    UPDATE book SET recent_order=0 WHERE id=old.book_id AND old.book_id!=new.book_id;
    UPDATE book SET recent_order=-new.updated_at WHERE id=new.book_id;
END;
CREATE TRIGGER activity_order_delete AFTER DELETE ON reading_activity BEGIN
    UPDATE book SET recent_order=0 WHERE id=old.book_id;
END;

-- Search-result cache invalidation is transactional and also covers other processes.
-- A fresh token avoids counter reuse after restoring an earlier database snapshot.
CREATE TABLE search_revision (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    token TEXT NOT NULL DEFAULT (lower(hex(randomblob(16))))
);
INSERT INTO search_revision(singleton) VALUES(1);
CREATE TRIGGER search_book_insert AFTER INSERT ON book BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_book_delete AFTER DELETE ON book BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_book_update
AFTER UPDATE OF id,metadata_json,search_text,review_state,language_key,work_type_key ON book BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_personal_insert AFTER INSERT ON personal BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_personal_delete AFTER DELETE ON personal BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_personal_update AFTER UPDATE ON personal
WHEN new.book_id IS NOT old.book_id
    OR json_extract(new.state_json,'$.rating') IS NOT json_extract(old.state_json,'$.rating')
    OR json_extract(new.state_json,'$.completed') IS NOT json_extract(old.state_json,'$.completed')
    OR (json_extract(new.state_json,'$.resume_page') IS NULL)
        != (json_extract(old.state_json,'$.resume_page') IS NULL)
BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_tag_insert AFTER INSERT ON book_tag BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_tag_update AFTER UPDATE ON book_tag BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_tag_delete AFTER DELETE ON book_tag BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_alias_insert AFTER INSERT ON tag_alias BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_alias_update AFTER UPDATE ON tag_alias BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
CREATE TRIGGER search_alias_delete AFTER DELETE ON tag_alias BEGIN
    UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1;
END;
-- Authentication and HTTP ------------------------------------------------
CREATE TABLE auth_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    passcode_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE login_attempt (
    client TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    attempts INTEGER NOT NULL CHECK(attempts >= 1)
);
CREATE TABLE credential (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('management','readonly','downloads','session')),
    label TEXT NOT NULL,
    secret_hash TEXT NOT NULL UNIQUE,
    previous_hash TEXT,
    previous_until INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    rotated_at INTEGER NOT NULL,
    expires_at INTEGER,
    revoked_at INTEGER
);
CREATE UNIQUE INDEX credential_previous_hash ON credential(previous_hash) WHERE previous_hash IS NOT NULL;
CREATE TABLE client_observation (
    credential_id TEXT NOT NULL REFERENCES credential(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    PRIMARY KEY(credential_id,book_id)
);
CREATE INDEX client_observation_book ON client_observation(book_id);
CREATE TABLE http_config (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    settings_json TEXT NOT NULL CHECK(json_valid(settings_json))
);

-- Sources and downloads --------------------------------------------------
CREATE TABLE source_config (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    settings_json TEXT NOT NULL CHECK(json_valid(settings_json)),
    secrets TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1)
);
CREATE TABLE source_observation (
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
    PRIMARY KEY(book_id,source_url)
);
CREATE TABLE source_check (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('queued','running','done','needs_review','ignored')),
    generation INTEGER NOT NULL DEFAULT 1,
    error_code TEXT,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    retry_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX source_check_queue ON source_check(state,updated_at,book_id);
CREATE TABLE source_note (
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    notes_json TEXT NOT NULL CHECK(json_valid(notes_json)),
    comments_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(comments_json)),
    PRIMARY KEY(book_id,source_url)
);
-- Site-wide request halts after a ban, a "too fast" warning or a 429, keyed by the site group's
-- first site.
CREATE TABLE source_halt (
    site TEXT PRIMARY KEY,
    until INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('ban','too_fast','http_429')),
    strikes INTEGER NOT NULL DEFAULT 1 CHECK(strikes>=1)
);
-- E(x)Hentai rate limits and the usage bucket as it stood when each one came.
CREATE TABLE source_rate_event (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    site TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('api','page','search','archiver')),
    reason TEXT NOT NULL CHECK(reason IN ('ban','too_fast','http_429')),
    until INTEGER NOT NULL,
    level REAL NOT NULL,
    remaining REAL NOT NULL,
    penalty REAL NOT NULL
);
-- Source work a rate limit put off, run in the background once `retry_at` passes: a metadata
-- refresh asked for by hand, or comments a fetch could not read (`manual`: asked for by hand, so
-- it is not held to the background share of the request budget).
CREATE TABLE source_task (
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('metadata','comments')),
    manual INTEGER NOT NULL CHECK(manual IN (0,1)),
    retry_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(book_id,source_url,kind)
);
-- E(x)Hentai's usage bucket and temporary penalty.
CREATE TABLE source_budget (
    site TEXT PRIMARY KEY,
    level REAL NOT NULL DEFAULT 0,
    updated REAL NOT NULL,
    penalty REAL NOT NULL DEFAULT 1,
    penalty_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE download_job (
    id TEXT PRIMARY KEY,
    input_url TEXT NOT NULL UNIQUE,
    source_json TEXT NOT NULL CHECK(json_valid(source_json)),
    state TEXT NOT NULL CHECK(state IN ('queued','inspecting','needs_approval',
        'submitting','downloading','importing','pausing','paused','cancelling','cancelled',
        'verification_required','error','done')),
    revision INTEGER NOT NULL DEFAULT 1,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
    remote_secret TEXT,
    received INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    book_id TEXT REFERENCES book(id) ON DELETE SET NULL,
    error_code TEXT,
    retry_at INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    phase TEXT GENERATED ALWAYS AS (json_extract(details_json,'$.phase')) VIRTUAL,
    gallery_key TEXT GENERATED ALWAYS AS (
        CASE WHEN json_extract(source_json,'$.site') IN ('ehentai','exhentai') THEN 'ehentai'
        ELSE json_extract(source_json,'$.site') END || ':' || json_extract(source_json,'$.gallery_id')
    ) VIRTUAL
);
CREATE INDEX download_job_queue ON download_job(state,retry_at,created_at) WHERE state='queued';
CREATE INDEX download_job_gallery ON download_job(gallery_key);
CREATE TABLE source_account (
    site_group TEXT PRIMARY KEY,
    balance_gp INTEGER NOT NULL,
    checked_at INTEGER NOT NULL
);
CREATE INDEX download_job_book ON download_job(book_id);
CREATE INDEX download_job_live ON download_job(state,created_at)
    WHERE state NOT IN ('done','cancelled');
CREATE INDEX download_job_retention ON download_job(updated_at) WHERE state IN ('done','cancelled');
CREATE INDEX download_job_completed ON download_job(updated_at) WHERE state='done';

-- Imports and scans ------------------------------------------------------
CREATE TABLE import_job (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('copying','ready','done','failed')),
    kind TEXT NOT NULL CHECK(kind IN ('copy','index')),
    durable INTEGER NOT NULL DEFAULT 0 CHECK(durable IN (0,1)),
    created INTEGER NOT NULL DEFAULT 0 CHECK(created IN (0,1)),
    publication_json TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    content_json TEXT,
    report_json TEXT,
    pages_json TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warnings_json)),
    error_code TEXT,
    book_id TEXT REFERENCES book(id) ON DELETE SET NULL
);
CREATE INDEX import_job_terminal ON import_job(created_at) WHERE state IN ('done','failed');
CREATE INDEX import_job_book ON import_job(book_id);
CREATE TABLE scan_run (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('active','paused','done')),
    created_at INTEGER NOT NULL,
    ignored INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    retry_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX scan_unfinished ON scan_run((1)) WHERE state!='done';
CREATE TABLE scan_policy (
    scan_id TEXT PRIMARY KEY REFERENCES scan_run(id) ON DELETE CASCADE,
    keep_source INTEGER NOT NULL CHECK(keep_source IN (0,1))
);
CREATE TABLE scan_item (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scan_run(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('directory','archive')),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','expanded','indexed','unchanged','duplicate','error')),
    book_id TEXT REFERENCES book(id) ON DELETE SET NULL,
    error_code TEXT,
    error_message TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warnings_json)),
    UNIQUE(scan_id,path)
);
CREATE INDEX scan_pending ON scan_item(scan_id,state,id);
CREATE INDEX scan_item_book ON scan_item(book_id);
CREATE TABLE import_receipt (
    source_path TEXT PRIMARY KEY,
    stamp_json TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE
);
CREATE INDEX import_receipt_book ON import_receipt(book_id);

-- Organization -----------------------------------------------------------
CREATE TABLE collection (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 256),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)
);
CREATE INDEX collection_name ON collection(name COLLATE NOCASE,id);
CREATE TABLE collection_item (
    collection_id TEXT NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(collection_id,book_id),
    UNIQUE(collection_id,position)
);
CREATE INDEX collection_book ON collection_item(book_id);
CREATE TABLE collection_rule (
    collection_id TEXT PRIMARY KEY REFERENCES collection(id) ON DELETE CASCADE,
    rule_json TEXT NOT NULL CHECK(json_valid(rule_json))
);
CREATE TABLE bulk_job (
    id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    state TEXT NOT NULL CHECK(state IN ('running','done','cancelled')),
    created_at INTEGER NOT NULL
);
CREATE INDEX bulk_job_running ON bulk_job(state,created_at);
CREATE TABLE bulk_item (
    job_id TEXT NOT NULL REFERENCES bulk_job(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    book_id TEXT NOT NULL,
    metadata_revision INTEGER NOT NULL,
    personal_revision INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','done','conflict','missing','error','cancelled','ignored')),
    PRIMARY KEY(job_id,position)
);

CREATE INDEX bulk_item_pending ON bulk_item(job_id,state,position);

-- Maintenance: removals and duplicate detection --------------------------
CREATE TABLE removal (
    id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    rows_json TEXT NOT NULL CHECK(json_valid(rows_json)),
    created_at INTEGER NOT NULL
);
CREATE TABLE maintenance_stamp (
    name TEXT PRIMARY KEY,
    updated_at INTEGER NOT NULL
);
CREATE TABLE page_feature (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    digest TEXT NOT NULL,
    dhash TEXT NOT NULL,
    gray BLOB NOT NULL,
    aspect REAL NOT NULL,
    failed INTEGER NOT NULL DEFAULT 0 CHECK(failed IN (0,1)),
    UNIQUE(book_id,page)
);
CREATE TRIGGER feature_insert AFTER INSERT ON page_feature BEGIN
    UPDATE book SET indexed_pages=indexed_pages+1 WHERE id=new.book_id;
END;
CREATE TRIGGER feature_delete AFTER DELETE ON page_feature BEGIN
    UPDATE book SET indexed_pages=indexed_pages-1 WHERE id=old.book_id;
END;
CREATE TABLE page_band (
    segment INTEGER NOT NULL,
    value INTEGER NOT NULL,
    feature_id INTEGER NOT NULL REFERENCES page_feature(id) ON DELETE CASCADE,
    PRIMARY KEY(segment,value,feature_id)
);
CREATE INDEX feature_bands ON page_band(feature_id);
CREATE TABLE duplicate_edge (
    left_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    right_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    left_page INTEGER NOT NULL,
    right_page INTEGER NOT NULL,
    PRIMARY KEY(left_id,right_id,left_page,right_page)
);
CREATE INDEX duplicate_edge_right ON duplicate_edge(right_id);
CREATE TABLE duplicate_decision (
    left_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    right_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    PRIMARY KEY(left_id,right_id)
);
CREATE TABLE duplicate_progress (
    book_id TEXT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE
);
CREATE TRIGGER duplicate_reset AFTER DELETE ON page_feature BEGIN
    DELETE FROM duplicate_progress WHERE book_id=old.book_id;
    DELETE FROM duplicate_edge WHERE left_id=old.book_id OR right_id=old.book_id;
END;
CREATE INDEX book_source_site ON book(source_site_key,id);
CREATE INDEX book_source_rating ON book(source_rating,id);
CREATE INDEX book_needs_review ON book(id) WHERE review_state='needs_review';
CREATE TABLE tag_count (
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY(namespace,value)
) WITHOUT ROWID;
CREATE TRIGGER tag_count_insert AFTER INSERT ON book_tag BEGIN
    INSERT INTO tag_count VALUES(new.namespace,new.value,1)
    ON CONFLICT(namespace,value) DO UPDATE SET count=count+1;
END;
CREATE TRIGGER tag_count_delete AFTER DELETE ON book_tag BEGIN
    UPDATE tag_count SET count=count-1 WHERE namespace=old.namespace AND value=old.value;
    DELETE FROM tag_count WHERE namespace=old.namespace AND value=old.value AND count=0;
END;
CREATE TABLE book_newer (
    book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    site_group TEXT NOT NULL,
    gallery_id INTEGER NOT NULL,
    PRIMARY KEY(book_id,site_group,gallery_id)
) WITHOUT ROWID;
CREATE INDEX book_newer_gallery ON book_newer(site_group,gallery_id,book_id);
CREATE INDEX duplicate_decision_right ON duplicate_decision(right_id);

"""
