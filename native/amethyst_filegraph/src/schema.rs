use crate::error::{FileGraphError, Result};
use crate::model::SCHEMA_VERSION;
use rusqlite::Connection;

pub const SCHEMA_SQL: &str = r#"
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS mods (
    mod_id INTEGER PRIMARY KEY,
    name_key TEXT NOT NULL UNIQUE,
    name_display TEXT NOT NULL,
    manifest_fingerprint BLOB NOT NULL DEFAULT X'',
    manifest_generation INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_files (
    file_id INTEGER PRIMARY KEY,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
    source_rel BLOB NOT NULL,
    source_display TEXT NOT NULL,
    index_display TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    ordinal INTEGER NOT NULL DEFAULT 0,
    flags INTEGER NOT NULL DEFAULT 0,
    UNIQUE(mod_id, source_rel)
);

CREATE TABLE IF NOT EXISTS archives (
    archive_id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES raw_files(file_id) ON DELETE CASCADE,
    archive_key TEXT NOT NULL,
    plugin_key TEXT,
    format TEXT,
    UNIQUE(file_id, archive_key)
);

CREATE TABLE IF NOT EXISTS archive_members (
    member_id INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL REFERENCES archives(archive_id) ON DELETE CASCADE,
    destination_id INTEGER,
    member_key BLOB NOT NULL,
    member_display TEXT NOT NULL,
    UNIQUE(archive_id, member_key)
);

CREATE TABLE IF NOT EXISTS route_variants (
    variant_id INTEGER PRIMARY KEY,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
    variant_key TEXT NOT NULL,
    rules_hash BLOB NOT NULL DEFAULT X'',
    UNIQUE(mod_id, variant_key)
);

-- The old mod index's post-strip spelling and UI capability flags depend on
-- profile routing rules. Keep that projection beside its route variant; a
-- shared library must not inherit whichever profile derived the mod last.
CREATE TABLE IF NOT EXISTS raw_variant_files (
    variant_id INTEGER NOT NULL REFERENCES route_variants(variant_id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES raw_files(file_id) ON DELETE CASCADE,
    index_display TEXT NOT NULL DEFAULT '',
    flags INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(variant_id, file_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS targets (
    target_id INTEGER PRIMARY KEY,
    target_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS destinations (
    destination_id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(target_id) ON DELETE CASCADE,
    path_key BLOB NOT NULL,
    path_display TEXT NOT NULL,
    UNIQUE(target_id, path_key)
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id INTEGER PRIMARY KEY,
    variant_id INTEGER NOT NULL REFERENCES route_variants(variant_id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES raw_files(file_id) ON DELETE CASCADE,
    destination_id INTEGER NOT NULL REFERENCES destinations(destination_id),
    provider_kind INTEGER NOT NULL,
    archive_key TEXT,
    plugin_key TEXT,
    deployable INTEGER NOT NULL DEFAULT 1,
    legacy_root INTEGER NOT NULL DEFAULT 0,
    legacy_rel TEXT NOT NULL,
    flags INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidate_identities (
    candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    identity_kind TEXT NOT NULL,
    identity_key BLOB NOT NULL,
    PRIMARY KEY(candidate_id, identity_kind, identity_key)
);

CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    intent_hash BLOB NOT NULL DEFAULT X'',
    intent_payload BLOB NOT NULL DEFAULT X'',
    rules_hash BLOB NOT NULL DEFAULT X'',
    generation INTEGER NOT NULL DEFAULT 0,
    inventory_generation INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile_mods (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL,
    order_label INTEGER NOT NULL,
    variant_id INTEGER REFERENCES route_variants(variant_id),
    PRIMARY KEY(profile_id, mod_id)
);

CREATE TABLE IF NOT EXISTS winners (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    namespace INTEGER NOT NULL,
    destination_id INTEGER NOT NULL REFERENCES destinations(destination_id),
    candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id),
    generation INTEGER NOT NULL,
    PRIMARY KEY(profile_id, namespace, destination_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS conflict_edges (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    conflict_kind TEXT NOT NULL,
    loser_mod_id INTEGER NOT NULL REFERENCES mods(mod_id),
    winner_mod_id INTEGER NOT NULL REFERENCES mods(mod_id),
    refcount INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY(profile_id, conflict_kind, loser_mod_id, winner_mod_id)
);

CREATE TABLE IF NOT EXISTS mod_summaries (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id),
    payload BLOB NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY(profile_id, mod_id)
);

CREATE TABLE IF NOT EXISTS deployed_entries (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    target_key TEXT NOT NULL,
    destination_key BLOB NOT NULL,
    destination_display TEXT NOT NULL,
    candidate_id INTEGER NOT NULL,
    mod_name TEXT NOT NULL,
    mod_key TEXT NOT NULL,
    provider_kind INTEGER NOT NULL,
    source_rel BLOB NOT NULL,
    source_display TEXT NOT NULL,
    source_fingerprint BLOB NOT NULL DEFAULT X'',
    link_mode TEXT NOT NULL,
    deployed_generation INTEGER NOT NULL,
    PRIMARY KEY(profile_id, target_key, destination_key)
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    profile_id TEXT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'planned',
    payload BLOB NOT NULL DEFAULT X'',
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_files_mod ON raw_files(mod_id);
CREATE INDEX IF NOT EXISTS idx_raw_files_mod_ordinal ON raw_files(mod_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_variants_mod ON route_variants(mod_id, variant_key);
CREATE INDEX IF NOT EXISTS idx_raw_variant_file ON raw_variant_files(file_id);
CREATE INDEX IF NOT EXISTS idx_candidates_variant ON candidates(variant_id);
-- Foreign-key cascades probe child keys once for every deleted parent row.
-- Without these indexes, removing/replacing a large manifest turns into an
-- O(files * candidates) scan even though the actual delete is local to one
-- mod.
CREATE INDEX IF NOT EXISTS idx_candidates_file ON candidates(file_id);
CREATE INDEX IF NOT EXISTS idx_candidates_destination ON candidates(destination_id);
CREATE INDEX IF NOT EXISTS idx_identities_key ON candidate_identities(identity_kind, identity_key);
CREATE INDEX IF NOT EXISTS idx_archive_members_destination ON archive_members(destination_id);
CREATE INDEX IF NOT EXISTS idx_profile_mods_mod ON profile_mods(mod_id);
CREATE INDEX IF NOT EXISTS idx_profile_mods_variant ON profile_mods(variant_id);
CREATE INDEX IF NOT EXISTS idx_winners_candidate ON winners(candidate_id);
CREATE INDEX IF NOT EXISTS idx_conflict_edges_loser ON conflict_edges(loser_mod_id);
CREATE INDEX IF NOT EXISTS idx_conflict_edges_winner ON conflict_edges(winner_mod_id);
CREATE INDEX IF NOT EXISTS idx_mod_summaries_mod ON mod_summaries(mod_id);
CREATE INDEX IF NOT EXISTS idx_operations_profile_state ON operations(profile_id, state);
"#;

pub fn configure_connection(connection: &Connection) -> Result<()> {
    connection.pragma_update(None, "foreign_keys", "ON")?;
    connection.pragma_update(None, "busy_timeout", 5_000_i64)?;
    connection.pragma_update(None, "synchronous", "NORMAL")?;
    connection.pragma_update(None, "wal_autocheckpoint", 16_384_i64)?;
    let mode: String = connection.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))?;
    if !mode.eq_ignore_ascii_case("wal") {
        connection.pragma_update(None, "journal_mode", "DELETE")?;
    }
    Ok(())
}

pub fn initialise(connection: &Connection) -> Result<()> {
    configure_connection(connection)?;
    let check: String = connection.query_row("PRAGMA quick_check(1)", [], |row| row.get(0))?;
    if check != "ok" {
        return Err(FileGraphError::Corrupt(check));
    }
    connection.execute_batch(SCHEMA_SQL)?;

    let found: u32 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if found > SCHEMA_VERSION {
        return Err(FileGraphError::Schema {
            found,
            expected: SCHEMA_VERSION,
        });
    }
    if found == 1 {
        connection.execute(
            "ALTER TABLE profiles ADD COLUMN intent_payload BLOB NOT NULL DEFAULT X''",
            [],
        )?;
    }
    let deployed_uses_integer_ids = if found > 0 && found < 3 {
        let mut statement = connection.prepare("PRAGMA table_info(deployed_entries)")?;
        let columns = statement.query_map([], |row| row.get::<_, String>(1))?;
        let mut legacy = false;
        for column in columns {
            legacy |= column? == "target_id";
        }
        legacy
    } else {
        false
    };
    if deployed_uses_integer_ids {
        connection.execute_batch(
            "ALTER TABLE deployed_entries RENAME TO deployed_entries_v2;
             CREATE TABLE deployed_entries (
                 profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
                 target_key TEXT NOT NULL,
                 destination_key BLOB NOT NULL,
                 destination_display TEXT NOT NULL,
                 candidate_id INTEGER NOT NULL,
                 mod_name TEXT NOT NULL,
                 mod_key TEXT NOT NULL,
                 provider_kind INTEGER NOT NULL,
                 source_rel BLOB NOT NULL,
                 source_display TEXT NOT NULL,
                 source_fingerprint BLOB NOT NULL DEFAULT X'',
                 link_mode TEXT NOT NULL,
                 deployed_generation INTEGER NOT NULL,
                 PRIMARY KEY(profile_id, target_key, destination_key)
             );
             INSERT INTO deployed_entries(
                 profile_id, target_key, destination_key, destination_display,
                 candidate_id, mod_name, mod_key, provider_kind, source_rel,
                 source_display, source_fingerprint, link_mode,
                 deployed_generation)
             SELECT de.profile_id, t.target_key, d.path_key, d.path_display,
                    de.candidate_id, m.name_display, m.name_key, c.provider_kind,
                    rf.source_rel, rf.source_display, de.source_fingerprint,
                    de.link_mode, de.deployed_generation
             FROM deployed_entries_v2 de
             JOIN targets t ON t.target_id=de.target_id
             JOIN destinations d ON d.destination_id=de.destination_id
             JOIN candidates c ON c.candidate_id=de.candidate_id
             JOIN route_variants rv ON rv.variant_id=c.variant_id
             JOIN mods m ON m.mod_id=rv.mod_id
             JOIN raw_files rf ON rf.file_id=c.file_id;
             DROP TABLE deployed_entries_v2;",
        )?;
    }
    if found == 3 {
        connection.execute(
            "ALTER TABLE deployed_entries ADD COLUMN mod_name TEXT NOT NULL DEFAULT ''",
            [],
        )?;
        connection.execute(
            "UPDATE deployed_entries SET mod_name=COALESCE((
                 SELECT m.name_display FROM candidates c
                 JOIN route_variants rv ON rv.variant_id=c.variant_id
                 JOIN mods m ON m.mod_id=rv.mod_id
                 WHERE c.candidate_id=deployed_entries.candidate_id
             ), mod_key) WHERE mod_name=''",
            [],
        )?;
    }
    if found == 4 {
        connection.execute(
            "ALTER TABLE operations ADD COLUMN phase TEXT NOT NULL DEFAULT 'planned'",
            [],
        )?;
        // ProfileIntent gained explicit volatile-provider variant keys in v5.
        // Avoid restoring a v4 graph that the first complete v5 intent must
        // immediately rebuild.
        connection.execute("UPDATE profiles SET ready=0", [])?;
    }
    if found > 0 && found < 6 {
        connection.execute_batch(
            "ALTER TABLE winners RENAME TO winners_rowid;
             CREATE TABLE winners (
                 profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
                 namespace INTEGER NOT NULL,
                 destination_id INTEGER NOT NULL REFERENCES destinations(destination_id),
                 candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id),
                 generation INTEGER NOT NULL,
                 PRIMARY KEY(profile_id, namespace, destination_id)
             ) WITHOUT ROWID;
             INSERT INTO winners(
                 profile_id, namespace, destination_id, candidate_id, generation)
             SELECT profile_id, namespace, destination_id, candidate_id, generation
             FROM winners_rowid;
             DROP TABLE winners_rowid;",
        )?;
    }
    if found > 0 && found < 7 {
        let mut statement = connection.prepare("PRAGMA table_info(raw_files)")?;
        let columns = statement.query_map([], |row| row.get::<_, String>(1))?;
        let mut has_flags = false;
        for column in columns {
            has_flags |= column? == "flags";
        }
        if !has_flags {
            connection.execute(
                "ALTER TABLE raw_files ADD COLUMN flags INTEGER NOT NULL DEFAULT 0",
                [],
            )?;
        }
    }
    if found > 0 && found < 8 {
        let mut statement = connection.prepare("PRAGMA table_info(raw_files)")?;
        let columns = statement.query_map([], |row| row.get::<_, String>(1))?;
        let mut has_index_display = false;
        for column in columns {
            has_index_display |= column? == "index_display";
        }
        if !has_index_display {
            connection.execute(
                "ALTER TABLE raw_files ADD COLUMN index_display TEXT NOT NULL DEFAULT ''",
                [],
            )?;
        }
    }
    if found > 0 && found < 9 {
        // SCHEMA_SQL has created the table above. Seed every pre-v9 variant
        // from the former global projection; RULES_REVISION then causes an
        // active profile to replace this approximation with exact data.
        connection.execute_batch(
            "INSERT OR IGNORE INTO raw_variant_files(
                 variant_id, file_id, index_display, flags)
             SELECT rv.variant_id, rf.file_id, rf.index_display, rf.flags
             FROM route_variants rv
             JOIN raw_files rf ON rf.mod_id=rv.mod_id;",
        )?;
    }

    // A deployment journal is recovery data, not history.  Early Filegraph
    // builds retained the complete serialized plan after success/failure;
    // large profiles consequently added tens of MiB on every deploy and made
    // quick_check/startup progressively slower.  Reclaim that one-time bloat
    // while upgrading/opening an affected development catalog.
    let reclaim_bytes = connection
        .query_row(
            "SELECT COALESCE(SUM(length(payload)), 0) FROM operations \
         WHERE state IN ('committed', 'failed')",
            [],
            |row| row.get::<_, i64>(0),
        )?
        .max(0) as u64;
    connection.execute(
        "DELETE FROM operations WHERE state IN ('committed', 'failed')",
        [],
    )?;
    if reclaim_bytes >= 8 * 1024 * 1024 {
        connection.execute_batch("VACUUM")?;
    }
    if found < SCHEMA_VERSION {
        connection.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    }

    connection.execute(
        "INSERT INTO meta(key, value) VALUES('inventory_generation', ?1) \
         ON CONFLICT(key) DO NOTHING",
        [0_u64.to_le_bytes().as_slice()],
    )?;
    write_u64_meta(connection, "engine_revision", crate::model::ENGINE_REVISION)?;
    write_u64_meta(connection, "rules_revision", crate::model::RULES_REVISION)?;
    connection.execute(
        "INSERT INTO meta(key, value) VALUES('ready', X'00') \
         ON CONFLICT(key) DO NOTHING",
        [],
    )?;
    Ok(())
}

pub fn read_u64_meta(connection: &Connection, key: &str) -> Result<u64> {
    let bytes: Vec<u8> =
        connection.query_row("SELECT value FROM meta WHERE key=?1", [key], |row| {
            row.get(0)
        })?;
    let array: [u8; 8] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| FileGraphError::Corrupt(format!("invalid u64 meta value for {key}")))?;
    Ok(u64::from_le_bytes(array))
}

pub fn write_u64_meta(connection: &Connection, key: &str, value: u64) -> Result<()> {
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?1, ?2) \
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        rusqlite::params![key, value.to_le_bytes().as_slice()],
    )?;
    Ok(())
}
