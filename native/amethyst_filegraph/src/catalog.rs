use crate::error::{FileGraphError, Result};
use crate::graph::{GraphSnapshot, GraphUpdate, reconcile_graph};
use crate::model::{
    API_VERSION, Candidate, CandidateRecord, CatalogStatus, DeployedStateRecord, DeploymentJournal,
    DeploymentPlanRecord, ManifestBatch, Namespace, OperationRecord, ProfileIntent, ProviderKind,
    RawCatalogFile, RawFileRecord, ResolutionDelta, SCHEMA_VERSION,
};
use crate::schema::{initialise, read_u64_meta, write_u64_meta};
use fs2::FileExt;
use parking_lot::{Mutex, RwLock};
use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension, params, params_from_iter};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Instant;
use std::time::{SystemTime, UNIX_EPOCH};

fn intern_str(values: &mut HashSet<Arc<str>>, value: String) -> Arc<str> {
    if let Some(existing) = values.get(value.as_str()) {
        return existing.clone();
    }
    let value: Arc<str> = Arc::from(value);
    values.insert(value.clone());
    value
}

fn intern_bytes(values: &mut HashSet<Arc<[u8]>>, value: Vec<u8>) -> Arc<[u8]> {
    if let Some(existing) = values.get(value.as_slice()) {
        return existing.clone();
    }
    let value: Arc<[u8]> = Arc::from(value);
    values.insert(value.clone());
    value
}

fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .try_into()
        .unwrap_or(u64::MAX)
}

/// Remove disposable persisted rows which reference a mod's current catalog
/// identities before its candidates or the mod itself are replaced/deleted.
///
/// The in-memory ProfileCore keeps its immutable previous snapshot, so an
/// active profile can still calculate a precise incremental delta. Profiles
/// which are not currently active are marked stale and rebuild on next open.
fn invalidate_persisted_mod_state(connection: &Connection, mod_id: i64) -> Result<()> {
    connection.execute(
        "UPDATE profiles SET ready=0 WHERE profile_id IN (\
           SELECT profile_id FROM profile_mods WHERE mod_id=?1)",
        [mod_id],
    )?;
    connection.execute(
        "DELETE FROM winners WHERE candidate_id IN (\
           SELECT c.candidate_id FROM candidates c \
           JOIN route_variants rv ON rv.variant_id=c.variant_id \
           WHERE rv.mod_id=?1)",
        [mod_id],
    )?;
    connection.execute(
        "DELETE FROM conflict_edges WHERE loser_mod_id=?1 OR winner_mod_id=?1",
        [mod_id],
    )?;
    connection.execute("DELETE FROM mod_summaries WHERE mod_id=?1", [mod_id])?;
    connection.execute("DELETE FROM profile_mods WHERE mod_id=?1", [mod_id])?;
    Ok(())
}

fn selected_variant_pairs(intent: &ProfileIntent) -> Vec<(String, String)> {
    let mut selected: Vec<_> = intent
        .mods
        .iter()
        .map(|entry| (entry.key.clone(), entry.variant_key.clone()))
        .chain(
            intent
                .special_variants
                .iter()
                .map(|(key, variant)| (key.clone(), variant.clone())),
        )
        .collect();
    selected.sort_unstable();
    selected.dedup();
    selected
}

fn changed_selection_keys(
    connection: &Connection,
    cached_generation: u64,
    cached: &[(String, String)],
    selected: &[(String, String)],
) -> Result<HashSet<String>> {
    let old: HashMap<_, _> = cached
        .iter()
        .map(|(key, variant)| (key.as_str(), variant.as_str()))
        .collect();
    let new: HashMap<_, _> = selected
        .iter()
        .map(|(key, variant)| (key.as_str(), variant.as_str()))
        .collect();
    let mut changed = HashSet::new();
    for key in old.keys().chain(new.keys()) {
        if old.get(key) != new.get(key) {
            changed.insert((*key).to_owned());
        }
    }

    // Manifest replacement keeps the selected (mod, variant) pair unchanged,
    // so selection comparison alone cannot see a reinstall. The catalog's
    // per-mod mutation generation identifies those keys without rescanning
    // candidates belonging to every other mod.
    let mut available = HashSet::new();
    let mut statement = connection.prepare(
        "SELECT m.name_key, rv.variant_key, m.manifest_generation \
         FROM route_variants rv JOIN mods m ON m.mod_id=rv.mod_id",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)? as u64,
        ))
    })?;
    for row in rows {
        let (key, variant, manifest_generation) = row?;
        if manifest_generation > cached_generation {
            changed.insert(key.clone());
        }
        available.insert((key, variant));
    }
    for (key, variant) in selected {
        if !available.contains(&(key.clone(), variant.clone())) {
            changed.insert(key.clone());
        }
    }
    Ok(changed)
}

#[derive(Clone, Copy, Debug)]
struct PersistedProfileMod {
    mod_id: i64,
    enabled: bool,
    order_label: i64,
    variant_id: Option<i64>,
}

/// Assign descending, widely-spaced labels while preserving every label that
/// is still valid in the new order.  A local move temporarily unpins only the
/// moved rows, so the common case writes one row (or one moved block) instead
/// of rewriting the whole profile.
fn profile_order_labels(
    intent: &ProfileIntent,
    existing: &HashMap<String, PersistedProfileMod>,
) -> Vec<i64> {
    const UPPER: i64 = 1_i64 << 62;
    const LOWER: i64 = -(1_i64 << 62);

    let moved: HashSet<String> = if matches!(intent.hint.kind.as_str(), "move" | "move_block") {
        intent
            .hint
            .mods
            .iter()
            .map(|name| name.to_lowercase())
            .collect()
    } else {
        HashSet::new()
    };
    let mut labels: Vec<Option<i64>> = intent
        .mods
        .iter()
        .map(|entry| {
            if moved.contains(&entry.key) {
                None
            } else {
                existing.get(&entry.key).map(|row| row.order_label)
            }
        })
        .collect();

    // Fixed labels must already be in descending order. An arbitrary external
    // order replacement can invert them; in that case relabel the profile once.
    let fixed_are_ordered = labels
        .iter()
        .flatten()
        .try_fold(None, |previous, &label| match previous {
            Some(value) if value <= label => Err(()),
            _ => Ok(Some(label)),
        })
        .is_ok();
    if !fixed_are_ordered {
        labels.fill(None);
    }

    let mut index = 0;
    while index < labels.len() {
        if labels[index].is_some() {
            index += 1;
            continue;
        }
        let start = index;
        while index < labels.len() && labels[index].is_none() {
            index += 1;
        }
        let end = index;
        let upper = if start == 0 {
            UPPER
        } else {
            labels[start - 1].unwrap()
        };
        let lower = if end == labels.len() {
            LOWER
        } else {
            labels[end].unwrap()
        };
        let count = (end - start) as i128;
        let gap = i128::from(upper) - i128::from(lower);
        if gap <= count {
            // Practically unreachable with 64-bit labels, but a locally dense
            // interval must never produce duplicate ordering keys.
            labels.fill(None);
            index = 0;
            continue;
        }
        let step = gap / (count + 1);
        for offset in 0..(end - start) {
            labels[start + offset] = Some((i128::from(upper) - step * (offset as i128 + 1)) as i64);
        }
    }
    labels.into_iter().map(Option::unwrap).collect()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct FileIdentity {
    device: u64,
    inode: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct CatalogPathIdentity {
    parent: FileIdentity,
    lock: FileIdentity,
    database: FileIdentity,
}

fn file_identity(path: &Path) -> Result<FileIdentity> {
    let metadata = std::fs::metadata(path)?;
    Ok(FileIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

fn catalog_path_identity(database_path: &Path) -> Result<CatalogPathIdentity> {
    let parent = database_path.parent().ok_or_else(|| {
        FileGraphError::Invalid(format!(
            "database has no parent: {}",
            database_path.display()
        ))
    })?;
    Ok(CatalogPathIdentity {
        parent: file_identity(parent)?,
        lock: file_identity(&parent.join("filegraph.lock"))?,
        database: file_identity(database_path)?,
    })
}

pub struct LibraryCore {
    pub database_path: PathBuf,
    lock_file: Mutex<File>,
    catalog_path_identity: Mutex<CatalogPathIdentity>,
    // Keep WAL ownership alive across short native calls. Without this,
    // closing the last connection after every generation forces repeated
    // checkpoints and creates large p95 spikes on local conflict updates.
    keeper: Mutex<Option<Connection>>,
    // The library owns an exclusive advisory lock, so inventory mutations can
    // only arrive through this core. Keep the generation in memory to avoid
    // opening three read connections and querying meta on every warm edit.
    inventory_generation: AtomicU64,
    candidate_cache: RwLock<Option<(u64, Arc<Vec<(String, String)>>, Arc<Vec<Candidate>>)>>,
    raw_file_cache: RwLock<Option<(u64, Arc<Vec<(String, String)>>, Arc<Vec<RawCatalogFile>>)>>,
}

impl LibraryCore {
    /// Reattach a long-lived library session when its profile-specific root
    /// was replaced in place (for example while creating/importing a profile).
    /// SQLite connections keep the deleted inode alive, while fresh read
    /// connections otherwise create an empty 4 KiB database at the new path
    /// and fail with `no such table: meta`.
    fn ensure_catalog_path_current(&self) -> Result<()> {
        let expected = *self.catalog_path_identity.lock();
        if catalog_path_identity(&self.database_path).ok() == Some(expected) {
            return Ok(());
        }

        let mut keeper = self.keeper.lock();
        let expected = *self.catalog_path_identity.lock();
        if catalog_path_identity(&self.database_path).ok() == Some(expected) {
            return Ok(());
        }

        let parent = self.database_path.parent().ok_or_else(|| {
            FileGraphError::Invalid(format!(
                "database has no parent: {}",
                self.database_path.display()
            ))
        })?;
        std::fs::create_dir_all(parent)?;
        let lock_path = parent.join("filegraph.lock");
        let current_lock = file_identity(&lock_path).ok();
        let replacement_lock = if current_lock != Some(expected.lock) {
            let lock = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .open(&lock_path)?;
            FileExt::try_lock_exclusive(&lock).map_err(|error| {
                FileGraphError::Busy(format!("{} ({error})", lock_path.display()))
            })?;
            Some(lock)
        } else {
            None
        };

        let connection = Connection::open(&self.database_path)?;
        initialise(&connection)?;
        let inventory_generation = read_u64_meta(&connection, "inventory_generation")?;
        let identity = catalog_path_identity(&self.database_path)?;

        drop(keeper.take());
        *keeper = Some(connection);
        if let Some(lock) = replacement_lock {
            *self.lock_file.lock() = lock;
        }
        *self.catalog_path_identity.lock() = identity;
        self.inventory_generation
            .store(inventory_generation, Ordering::Release);
        *self.candidate_cache.write() = None;
        *self.raw_file_cache.write() = None;
        Ok(())
    }

    fn ensure_no_active_operations(&self, connection: &Connection) -> Result<()> {
        let active: Option<String> = connection
            .query_row(
                "SELECT operation_id FROM operations WHERE state IN ('planned', 'mutating') LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(operation_id) = active {
            return Err(FileGraphError::Busy(format!(
                "deployment operation {operation_id} is still active"
            )));
        }
        Ok(())
    }

    fn ensure_profile_no_active_operations(&self, profile_id: &str) -> Result<()> {
        let keeper = self.keeper.lock();
        let connection = keeper
            .as_ref()
            .ok_or_else(|| FileGraphError::Busy("catalog writer is unavailable".to_owned()))?;
        let active: Option<String> = connection
            .query_row(
                "SELECT operation_id FROM operations WHERE profile_id=?1 \
                 AND state IN ('planned', 'mutating') LIMIT 1",
                [profile_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(operation_id) = active {
            return Err(FileGraphError::Busy(format!(
                "profile deployment {operation_id} is still active"
            )));
        }
        Ok(())
    }

    pub fn open(database_path: PathBuf) -> Result<Arc<Self>> {
        let parent = database_path.parent().ok_or_else(|| {
            FileGraphError::Invalid(format!(
                "database has no parent: {}",
                database_path.display()
            ))
        })?;
        std::fs::create_dir_all(parent)?;
        let lock_path = parent.join("filegraph.lock");
        let lock_file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(&lock_path)?;
        FileExt::try_lock_exclusive(&lock_file)
            .map_err(|error| FileGraphError::Busy(format!("{} ({error})", lock_path.display())))?;

        let connection = Connection::open(&database_path)?;
        initialise(&connection)?;
        let inventory_generation = read_u64_meta(&connection, "inventory_generation")?;
        let catalog_path_identity = catalog_path_identity(&database_path)?;
        let core = Arc::new(Self {
            database_path,
            lock_file: Mutex::new(lock_file),
            catalog_path_identity: Mutex::new(catalog_path_identity),
            keeper: Mutex::new(Some(connection)),
            inventory_generation: AtomicU64::new(inventory_generation),
            candidate_cache: RwLock::new(None),
            raw_file_cache: RwLock::new(None),
        });
        Ok(core)
    }

    pub fn connection(&self) -> Result<Connection> {
        let connection = Connection::open(&self.database_path)?;
        crate::schema::configure_connection(&connection)?;
        Ok(connection)
    }

    pub fn checkpoint(&self) -> Result<()> {
        let mut keeper = self.keeper.lock();
        let connection = match keeper.take() {
            Some(connection) => connection,
            None => self.connection()?,
        };
        let checkpointed = (|| -> Result<()> {
            let _: (i64, i64, i64) =
                connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| {
                    Ok((row.get(0)?, row.get(1)?, row.get(2)?))
                })?;
            let _: String =
                connection.query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))?;
            connection.pragma_update(None, "synchronous", "FULL")?;
            Ok(())
        })();
        drop(connection);
        let synced = File::open(&self.database_path)
            .and_then(|file| file.sync_all())
            .map_err(FileGraphError::from);
        let reopened = self.connection()?;
        *keeper = Some(reopened);
        checkpointed.and(synced)
    }

    pub fn activate_catalog(&self, source_database: &Path) -> Result<()> {
        if source_database == self.database_path {
            return Err(FileGraphError::Invalid(
                "replacement catalog must be a distinct sibling database".to_owned(),
            ));
        }
        let mut keeper = self.keeper.lock();
        let old_generation = {
            let connection = keeper
                .as_ref()
                .ok_or_else(|| FileGraphError::Busy("catalog keeper is unavailable".to_owned()))?;
            self.ensure_no_active_operations(connection)?;
            read_u64_meta(connection, "inventory_generation")?
        };

        let source = Connection::open(source_database)?;
        crate::schema::configure_connection(&source)?;
        let check: String = source.query_row("PRAGMA quick_check(1)", [], |row| row.get(0))?;
        if check != "ok" {
            return Err(FileGraphError::Corrupt(check));
        }
        let ready = source
            .query_row("SELECT value FROM meta WHERE key='ready'", [], |row| {
                row.get::<_, Vec<u8>>(0)
            })?
            .first()
            .copied()
            .unwrap_or(0)
            != 0;
        if !ready {
            return Err(FileGraphError::Invalid(
                "replacement catalog is not complete".to_owned(),
            ));
        }
        let replacement_generation = read_u64_meta(&source, "inventory_generation")?;
        let activated_generation = replacement_generation.max(old_generation.saturating_add(1));
        write_u64_meta(&source, "inventory_generation", activated_generation)?;
        let _: (i64, i64, i64) =
            source.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })?;
        let _: String = source.query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))?;
        source.pragma_update(None, "synchronous", "FULL")?;
        drop(source);
        File::open(source_database)?.sync_all()?;

        drop(keeper.take());
        let activation = (|| -> Result<()> {
            for suffix in ["-wal", "-shm"] {
                let companion =
                    PathBuf::from(format!("{}{}", self.database_path.display(), suffix));
                match std::fs::remove_file(companion) {
                    Ok(()) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
            }
            std::fs::rename(source_database, &self.database_path)?;
            if let Some(parent) = self.database_path.parent() {
                File::open(parent)?.sync_all()?;
            }
            Ok(())
        })();
        let reopened = self.connection();
        *keeper = Some(reopened?);
        activation?;
        *self.catalog_path_identity.lock() = catalog_path_identity(&self.database_path)?;
        self.inventory_generation
            .store(activated_generation, Ordering::Release);
        *self.candidate_cache.write() = None;
        *self.raw_file_cache.write() = None;
        Ok(())
    }

    pub fn status(&self) -> Result<CatalogStatus> {
        self.ensure_catalog_path_current()?;
        let connection = self.connection()?;
        let inventory_generation = read_u64_meta(&connection, "inventory_generation")?;
        let ready = connection
            .query_row("SELECT value FROM meta WHERE key='ready'", [], |row| {
                row.get::<_, Vec<u8>>(0)
            })
            .optional()?
            .and_then(|value| value.first().copied())
            .unwrap_or(0)
            != 0;
        let mod_count = connection
            .query_row("SELECT COUNT(*) FROM mods", [], |row| row.get::<_, i64>(0))?
            .try_into()
            .map_err(|_| FileGraphError::Corrupt("negative mod count".to_owned()))?;
        let candidate_count = connection
            .query_row("SELECT COUNT(*) FROM candidates", [], |row| {
                row.get::<_, i64>(0)
            })?
            .try_into()
            .map_err(|_| FileGraphError::Corrupt("negative candidate count".to_owned()))?;
        Ok(CatalogStatus {
            schema_version: SCHEMA_VERSION,
            api_version: API_VERSION,
            inventory_generation,
            engine_revision: crate::model::ENGINE_REVISION,
            rules_revision: crate::model::RULES_REVISION,
            ready,
            mod_count,
            candidate_count,
        })
    }

    pub fn mod_names(&self) -> Result<Vec<String>> {
        let connection = self.connection()?;
        let mut statement =
            connection.prepare("SELECT name_display FROM mods ORDER BY name_key")?;
        let rows = statement.query_map([], |row| row.get(0))?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    pub fn manifest_fingerprints(&self) -> Result<BTreeMap<String, Vec<u8>>> {
        let connection = self.connection()?;
        let mut statement = connection
            .prepare("SELECT name_display, manifest_fingerprint FROM mods ORDER BY name_key")?;
        let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
        let mut result = BTreeMap::new();
        for row in rows {
            let (name, fingerprint) = row?;
            result.insert(name, fingerprint);
        }
        Ok(result)
    }

    pub fn manifest_for_rederive(&self, mod_key: &str) -> Result<Option<ManifestBatch>> {
        let connection = self.connection()?;
        let metadata: Option<(i64, String, Vec<u8>)> = connection
            .query_row(
                "SELECT mod_id, name_display, manifest_fingerprint FROM mods WHERE name_key=?1",
                [mod_key],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        let Some((mod_id, mod_name, fingerprint)) = metadata else {
            return Ok(None);
        };
        let variant: Option<(i64, String)> = connection
            .query_row(
                "SELECT variant_id, variant_key FROM route_variants WHERE mod_id=?1 \
                 ORDER BY variant_id DESC LIMIT 1",
                [mod_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;

        let mut raw_statement = connection.prepare(
            "SELECT source_rel, source_display, index_display, size, mtime_ns, ordinal, flags \
             FROM raw_files WHERE mod_id=?1 ORDER BY ordinal, file_id",
        )?;
        let raw_rows = raw_statement.query_map([mod_id], |row| {
            Ok(RawFileRecord {
                source_rel: row.get(0)?,
                source_display: row.get(1)?,
                index_display: row.get(2)?,
                size: row.get::<_, i64>(3)? as u64,
                mtime_ns: row.get(4)?,
                ordinal: row.get::<_, i64>(5)? as u32,
                flags: row.get::<_, i64>(6)? as u32,
            })
        })?;
        let mut raw_files = Vec::new();
        for row in raw_rows {
            raw_files.push(row?);
        }

        let Some((variant_id, variant_key)) = variant else {
            return Ok(Some(ManifestBatch {
                mod_name,
                mod_key: mod_key.to_owned(),
                variant_key: String::new(),
                manifest_fingerprint: fingerprint,
                raw_files,
                candidates: Vec::new(),
            }));
        };
        let mut identities: HashMap<i64, Vec<Vec<u8>>> = HashMap::new();
        {
            let mut identity_statement = connection.prepare(
                "SELECT ci.candidate_id, ci.identity_key FROM candidate_identities ci \
                 JOIN candidates c ON c.candidate_id=ci.candidate_id \
                 WHERE c.variant_id=?1 ORDER BY ci.candidate_id",
            )?;
            let rows = identity_statement.query_map([variant_id], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, Vec<u8>>(1)?))
            })?;
            for row in rows {
                let (candidate_id, identity) = row?;
                identities.entry(candidate_id).or_default().push(identity);
            }
        }
        let mut candidate_statement = connection.prepare(
            "SELECT c.candidate_id, rf.source_rel, rf.source_display, t.target_key, \
                    d.path_key, d.path_display, c.provider_kind, rf.size, rf.mtime_ns, \
                    rf.ordinal, c.archive_key, c.plugin_key, c.deployable, c.legacy_root, \
                    c.legacy_rel, c.flags \
             FROM candidates c JOIN raw_files rf ON rf.file_id=c.file_id \
             JOIN destinations d ON d.destination_id=c.destination_id \
             JOIN targets t ON t.target_id=d.target_id \
             WHERE c.variant_id=?1 ORDER BY c.candidate_id",
        )?;
        let rows = candidate_statement.query_map([variant_id], |row| {
            let candidate_id: i64 = row.get(0)?;
            Ok(CandidateRecord {
                source_rel: row.get(1)?,
                source_display: row.get(2)?,
                target: row.get(3)?,
                destination_key: row.get(4)?,
                destination_display: row.get(5)?,
                kind: ProviderKind::from_i64(row.get(6)?),
                size: row.get::<_, i64>(7)? as u64,
                mtime_ns: row.get(8)?,
                ordinal: row.get::<_, i64>(9)? as u32,
                identities: identities.remove(&candidate_id).unwrap_or_default(),
                archive_key: row.get(10)?,
                plugin_key: row.get(11)?,
                deployable: row.get::<_, i64>(12)? != 0,
                legacy_root: row.get::<_, i64>(13)? != 0,
                legacy_rel: row.get(14)?,
                flags: row.get::<_, i64>(15)? as u32,
            })
        })?;
        let mut candidates = Vec::new();
        for row in rows {
            candidates.push(row?);
        }
        Ok(Some(ManifestBatch {
            mod_name,
            mod_key: mod_key.to_owned(),
            variant_key,
            manifest_fingerprint: fingerprint,
            raw_files,
            candidates,
        }))
    }

    pub fn variant_keys(&self) -> Result<BTreeMap<String, Vec<String>>> {
        let connection = self.connection()?;
        let mut statement = connection.prepare(
            "SELECT m.name_key, rv.variant_key FROM route_variants rv \
             JOIN mods m ON m.mod_id=rv.mod_id ORDER BY m.name_key, rv.variant_key",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut result: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for row in rows {
            let (mod_key, variant_key) = row?;
            result.entry(mod_key).or_default().push(variant_key);
        }
        Ok(result)
    }

    pub fn archive_units(
        &self,
        selected: &[(String, String)],
    ) -> Result<Vec<(String, String, String, String, Option<String>)>> {
        if selected.is_empty() {
            return Ok(Vec::new());
        }
        let connection = self.connection()?;
        let mut variant_ids = Vec::with_capacity(selected.len());
        {
            let mut find_variant = connection.prepare_cached(
                "SELECT rv.variant_id FROM route_variants rv \
                 JOIN mods m ON m.mod_id=rv.mod_id \
                 WHERE m.name_key=?1 AND rv.variant_key=?2",
            )?;
            for (key, variant) in selected {
                if let Some(variant_id) = find_variant
                    .query_row(params![key, variant], |row| row.get::<_, i64>(0))
                    .optional()?
                {
                    variant_ids.push(variant_id);
                }
            }
        }
        if variant_ids.is_empty() {
            return Ok(Vec::new());
        }
        let placeholders = std::iter::repeat_n("?", variant_ids.len())
            .collect::<Vec<_>>()
            .join(",");
        let query = format!(
            "SELECT DISTINCT m.name_display, m.name_key, c.archive_key, \
                    rf.source_display, c.plugin_key \
             FROM candidates c JOIN route_variants rv ON rv.variant_id=c.variant_id \
             JOIN mods m ON m.mod_id=rv.mod_id JOIN raw_files rf ON rf.file_id=c.file_id \
             WHERE c.provider_kind={} AND c.archive_key IS NOT NULL \
               AND c.variant_id IN ({placeholders}) \
             ORDER BY m.name_key, c.archive_key",
            ProviderKind::ArchiveMember.as_i64(),
        );
        let mut statement = connection.prepare(&query)?;
        let rows = statement.query_map(params_from_iter(variant_ids), |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    pub fn set_ready(&self, ready: bool) -> Result<()> {
        let connection = self.connection()?;
        let value = [u8::from(ready)];
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('ready', ?1) \
             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [value.as_slice()],
        )?;
        Ok(())
    }

    pub fn reset_catalog(&self) -> Result<()> {
        let mut connection = self.connection()?;
        self.ensure_no_active_operations(&connection)?;
        let transaction = connection.transaction()?;
        let next_generation =
            read_u64_meta(&transaction, "inventory_generation")?.saturating_add(1);
        transaction.execute("UPDATE meta SET value=X'00' WHERE key='ready'", [])?;
        transaction.execute("DELETE FROM operations", [])?;
        transaction.execute("DELETE FROM profiles", [])?;
        transaction.execute("DELETE FROM mods", [])?;
        // Inventory generations are monotonic even across a rebuild. Existing
        // ProfileSession objects can safely detect that their immutable graph
        // references predate the replacement catalog.
        write_u64_meta(&transaction, "inventory_generation", next_generation)?;
        transaction.commit()?;
        self.inventory_generation
            .store(next_generation, Ordering::Release);
        *self.candidate_cache.write() = None;
        *self.raw_file_cache.write() = None;
        Ok(())
    }

    pub fn replace_manifest(&self, batch: ManifestBatch, cancelled: &AtomicBool) -> Result<u64> {
        if batch.mod_key.is_empty() || batch.variant_key.is_empty() {
            return Err(FileGraphError::Invalid(
                "manifest mod_key and variant_key must not be empty".to_owned(),
            ));
        }
        let mut connection = self.connection()?;
        self.ensure_no_active_operations(&connection)?;
        let transaction = connection.transaction()?;

        let check_cancelled = || -> Result<()> {
            if cancelled.load(Ordering::Relaxed) {
                Err(FileGraphError::Invalid("operation cancelled".to_owned()))
            } else {
                Ok(())
            }
        };
        check_cancelled()?;

        transaction.execute(
            "INSERT INTO mods(name_key, name_display, manifest_fingerprint) VALUES(?1, ?2, ?3) \
             ON CONFLICT(name_key) DO UPDATE SET name_display=excluded.name_display",
            params![batch.mod_key, batch.mod_name, batch.manifest_fingerprint],
        )?;
        let mod_id: i64 = transaction.query_row(
            "SELECT mod_id FROM mods WHERE name_key=?1",
            [&batch.mod_key],
            |row| row.get(0),
        )?;
        invalidate_persisted_mod_state(&transaction, mod_id)?;
        let old_fingerprint: Vec<u8> = transaction.query_row(
            "SELECT manifest_fingerprint FROM mods WHERE mod_id=?1",
            [mod_id],
            |row| row.get(0),
        )?;
        if old_fingerprint != batch.manifest_fingerprint {
            transaction.execute("DELETE FROM raw_files WHERE mod_id=?1", [mod_id])?;
            transaction.execute("DELETE FROM route_variants WHERE mod_id=?1", [mod_id])?;
            transaction.execute(
                "UPDATE mods SET manifest_fingerprint=?1 WHERE mod_id=?2",
                params![batch.manifest_fingerprint, mod_id],
            )?;
        }

        transaction.execute(
            "INSERT INTO route_variants(mod_id, variant_key) VALUES(?1, ?2) \
             ON CONFLICT(mod_id, variant_key) DO NOTHING",
            params![mod_id, batch.variant_key],
        )?;
        let variant_id: i64 = transaction.query_row(
            "SELECT variant_id FROM route_variants WHERE mod_id=?1 AND variant_key=?2",
            params![mod_id, batch.variant_key],
            |row| row.get(0),
        )?;
        transaction.execute("DELETE FROM candidates WHERE variant_id=?1", [variant_id])?;
        transaction.execute(
            "DELETE FROM raw_variant_files WHERE variant_id=?1",
            [variant_id],
        )?;

        {
            let mut upsert_raw = transaction.prepare_cached(
                "INSERT INTO raw_files(mod_id, source_rel, source_display, index_display, size, mtime_ns, ordinal, flags) \
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8) \
                 ON CONFLICT(mod_id, source_rel) DO UPDATE SET \
                   source_display=excluded.source_display, size=excluded.size, \
                   mtime_ns=excluded.mtime_ns, ordinal=excluded.ordinal, \
                   flags=excluded.flags, index_display=excluded.index_display \
                 RETURNING file_id",
            )?;
            let mut upsert_target = transaction.prepare_cached(
                "INSERT INTO targets(target_key) VALUES(?1) \
                 ON CONFLICT(target_key) DO UPDATE SET target_key=excluded.target_key \
                 RETURNING target_id",
            )?;
            let mut upsert_destination = transaction.prepare_cached(
                "INSERT INTO destinations(target_id, path_key, path_display) VALUES(?1, ?2, ?3) \
                 ON CONFLICT(target_id, path_key) DO UPDATE SET path_display=excluded.path_display \
                 RETURNING destination_id",
            )?;
            let mut upsert_archive = transaction.prepare_cached(
                "INSERT INTO archives(file_id, archive_key, plugin_key, format) \
                 VALUES(?1, ?2, ?3, ?4) ON CONFLICT(file_id, archive_key) \
                 DO UPDATE SET plugin_key=excluded.plugin_key, format=excluded.format \
                 RETURNING archive_id",
            )?;
            let mut upsert_archive_member = transaction.prepare_cached(
                "INSERT INTO archive_members(archive_id, destination_id, member_key, member_display) \
                 VALUES(?1, ?2, ?3, ?4) ON CONFLICT(archive_id, member_key) \
                 DO UPDATE SET destination_id=excluded.destination_id, \
                   member_display=excluded.member_display",
            )?;
            let mut insert_candidate = transaction.prepare_cached(
                "INSERT INTO candidates(variant_id, file_id, destination_id, provider_kind, \
                  archive_key, plugin_key, deployable, legacy_root, legacy_rel, flags) \
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10) \
                 RETURNING candidate_id",
            )?;
            let mut insert_identity = transaction.prepare_cached(
                "INSERT INTO candidate_identities(candidate_id, identity_kind, identity_key) \
                 VALUES(?1, 'exclusive', ?2)",
            )?;
            let mut insert_raw_variant = transaction.prepare_cached(
                "INSERT INTO raw_variant_files(variant_id, file_id, index_display, flags) \
                 VALUES(?1, ?2, ?3, ?4) \
                 ON CONFLICT(variant_id, file_id) DO UPDATE SET \
                   index_display=excluded.index_display, flags=excluded.flags",
            )?;
            let mut file_ids: HashMap<Vec<u8>, i64> = HashMap::with_capacity(batch.raw_files.len());
            for (index, record) in batch.raw_files.iter().enumerate() {
                if index & 4095 == 0 {
                    check_cancelled()?;
                }
                let file_id = upsert_raw.query_row(
                    params![
                        mod_id,
                        &record.source_rel,
                        &record.source_display,
                        &record.index_display,
                        record.size as i64,
                        record.mtime_ns,
                        record.ordinal as i64,
                        record.flags as i64,
                    ],
                    |row| row.get(0),
                )?;
                insert_raw_variant.execute(params![
                    variant_id,
                    file_id,
                    &record.index_display,
                    record.flags as i64,
                ])?;
                file_ids.insert(record.source_rel.clone(), file_id);
            }

            let mut target_ids: HashMap<String, i64> = HashMap::new();
            for (index, record) in batch.candidates.into_iter().enumerate() {
                if index & 4095 == 0 {
                    check_cancelled()?;
                }
                let file_id = if let Some(file_id) = file_ids.get(&record.source_rel) {
                    *file_id
                } else {
                    let file_id = upsert_raw.query_row(
                        params![
                            mod_id,
                            &record.source_rel,
                            &record.source_display,
                            &record.legacy_rel,
                            record.size as i64,
                            record.mtime_ns,
                            record.ordinal as i64,
                            record.flags as i64,
                        ],
                        |row| row.get(0),
                    )?;
                    insert_raw_variant.execute(params![
                        variant_id,
                        file_id,
                        &record.legacy_rel,
                        record.flags as i64,
                    ])?;
                    file_ids.insert(record.source_rel.clone(), file_id);
                    file_id
                };
                let target_id = if let Some(target_id) = target_ids.get(&record.target) {
                    *target_id
                } else {
                    let target_id = upsert_target.query_row([&record.target], |row| row.get(0))?;
                    target_ids.insert(record.target.clone(), target_id);
                    target_id
                };
                let destination_id: i64 = upsert_destination.query_row(
                    params![
                        target_id,
                        &record.destination_key,
                        &record.destination_display,
                    ],
                    |row| row.get(0),
                )?;
                if record.kind == ProviderKind::ArchiveMember {
                    let archive_key = record.archive_key.as_ref().ok_or_else(|| {
                        FileGraphError::Invalid(
                            "archive-member candidate is missing archive_key".to_owned(),
                        )
                    })?;
                    let format = record
                        .source_display
                        .rsplit_once('.')
                        .map(|(_, extension)| extension.to_lowercase())
                        .unwrap_or_default();
                    let archive_id: i64 = upsert_archive.query_row(
                        params![file_id, archive_key, &record.plugin_key, format],
                        |row| row.get(0),
                    )?;
                    let member_key = record.legacy_rel.to_lowercase().into_bytes();
                    upsert_archive_member.execute(params![
                        archive_id,
                        destination_id,
                        member_key,
                        &record.legacy_rel,
                    ])?;
                }
                let candidate_id: i64 = insert_candidate.query_row(
                    params![
                        variant_id,
                        file_id,
                        destination_id,
                        record.kind.as_i64(),
                        &record.archive_key,
                        &record.plugin_key,
                        record.deployable as i64,
                        record.legacy_root as i64,
                        &record.legacy_rel,
                        record.flags as i64,
                    ],
                    |row| row.get(0),
                )?;
                for identity in record.identities {
                    insert_identity.execute(params![candidate_id, identity])?;
                }
            }
        }

        check_cancelled()?;
        let generation = read_u64_meta(&transaction, "inventory_generation")?.saturating_add(1);
        write_u64_meta(&transaction, "inventory_generation", generation)?;
        transaction.execute(
            "UPDATE mods SET manifest_generation=?1 WHERE mod_id=?2",
            params![generation as i64, mod_id],
        )?;
        transaction.commit()?;
        self.inventory_generation
            .store(generation, Ordering::Release);
        // Keep the previous selection projection available. The next profile
        // reconcile patches only this manifest's candidates/raw files into it
        // using manifest_generation instead of reloading the whole library.
        Ok(generation)
    }

    pub fn remove_mod(&self, mod_key: &str) -> Result<bool> {
        let mut connection = self.connection()?;
        self.ensure_no_active_operations(&connection)?;
        let transaction = connection.transaction()?;
        let mod_id = transaction
            .query_row(
                "SELECT mod_id FROM mods WHERE name_key=?1",
                [mod_key],
                |row| row.get::<_, i64>(0),
            )
            .optional()?;
        if let Some(mod_id) = mod_id {
            invalidate_persisted_mod_state(&transaction, mod_id)?;
        }
        let changed = transaction.execute("DELETE FROM mods WHERE name_key=?1", [mod_key])? > 0;
        let generation = if changed {
            let generation = read_u64_meta(&transaction, "inventory_generation")?.saturating_add(1);
            write_u64_meta(&transaction, "inventory_generation", generation)?;
            Some(generation)
        } else {
            None
        };
        transaction.commit()?;
        if changed {
            self.inventory_generation
                .store(generation.unwrap(), Ordering::Release);
        }
        Ok(changed)
    }

    pub fn rename_mod(&self, old_key: &str, new_key: &str, new_display: &str) -> Result<bool> {
        let mut connection = self.connection()?;
        self.ensure_no_active_operations(&connection)?;
        let transaction = connection.transaction()?;
        let changed = transaction.execute(
            "UPDATE mods SET name_key=?1, name_display=?2 WHERE name_key=?3",
            params![new_key, new_display, old_key],
        )? > 0;
        let generation = if changed {
            let generation = read_u64_meta(&transaction, "inventory_generation")?.saturating_add(1);
            write_u64_meta(&transaction, "inventory_generation", generation)?;
            Some(generation)
        } else {
            None
        };
        transaction.commit()?;
        if changed {
            self.inventory_generation
                .store(generation.unwrap(), Ordering::Release);
        }
        Ok(changed)
    }

    pub fn load_candidates(&self, intent: &ProfileIntent) -> Result<Arc<Vec<Candidate>>> {
        let generation = self.inventory_generation.load(Ordering::Acquire);
        let selected = selected_variant_pairs(intent);
        let cached = self.candidate_cache.read().as_ref().map(
            |(cached_generation, cached_selection, candidates)| {
                (
                    *cached_generation,
                    cached_selection.clone(),
                    candidates.clone(),
                )
            },
        );
        if let Some((cached_generation, cached_selection, candidates)) = &cached
            && *cached_generation == generation
            && cached_selection.as_ref() == &selected
        {
            return Ok(candidates.clone());
        }
        let connection = self.connection()?;
        if selected.is_empty() {
            let candidates = Arc::new(Vec::new());
            *self.candidate_cache.write() =
                Some((generation, Arc::new(selected), candidates.clone()));
            return Ok(candidates);
        }
        let selected_variants: HashMap<_, _> = selected
            .iter()
            .map(|(key, variant)| (key.as_str(), variant.as_str()))
            .collect();
        let mut candidates = Vec::new();
        let query_selection = if let Some((cached_generation, cached_selection, cached_values)) =
            cached
        {
            let changed = changed_selection_keys(
                &connection,
                cached_generation,
                &cached_selection,
                &selected,
            )?;
            if changed.is_empty() {
                *self.candidate_cache.write() =
                    Some((generation, Arc::new(selected), cached_values.clone()));
                return Ok(cached_values);
            }
            candidates.extend(
                cached_values
                    .iter()
                    .filter(|candidate| {
                        !changed.contains(candidate.mod_key.as_ref())
                            && selected_variants
                                .get(candidate.mod_key.as_ref())
                                .is_some_and(|variant| *variant == candidate.variant_key.as_ref())
                    })
                    .cloned(),
            );
            selected
                .iter()
                .filter(|(key, _variant)| changed.contains(key))
                .cloned()
                .collect::<Vec<_>>()
        } else {
            selected.clone()
        };
        let mut variant_ids = Vec::with_capacity(query_selection.len());
        {
            let mut find_variant = connection.prepare_cached(
                "SELECT rv.variant_id FROM route_variants rv \
                 JOIN mods m ON m.mod_id=rv.mod_id \
                 WHERE m.name_key=?1 AND rv.variant_key=?2",
            )?;
            for (key, variant) in &query_selection {
                if let Some(variant_id) = find_variant
                    .query_row(params![key, variant], |row| row.get::<_, i64>(0))
                    .optional()?
                {
                    variant_ids.push(variant_id);
                }
            }
        }
        if variant_ids.is_empty() {
            let candidates = Arc::new(candidates);
            *self.candidate_cache.write() =
                Some((generation, Arc::new(selected), candidates.clone()));
            return Ok(candidates);
        }
        let raw_files = self.load_raw_files(intent)?;
        let raw_by_id: HashMap<_, _> = raw_files.iter().map(|file| (file.id, file)).collect();
        let mut strings: HashSet<Arc<str>> = HashSet::new();
        let mut bytes: HashSet<Arc<[u8]>> = HashSet::new();
        for raw in raw_files.iter() {
            strings.insert(raw.mod_name.clone());
            strings.insert(raw.mod_key.clone());
            strings.insert(raw.source_display.clone());
            strings.insert(raw.index_display.clone());
            bytes.insert(raw.source_rel.clone());
        }
        let placeholders = std::iter::repeat_n("?", variant_ids.len())
            .collect::<Vec<_>>()
            .join(",");
        let mut identities: HashMap<i64, Vec<Arc<[u8]>>> = HashMap::new();
        {
            let identity_query = format!(
                "SELECT ci.candidate_id, ci.identity_key \
                 FROM candidate_identities ci \
                 JOIN candidates c ON c.candidate_id=ci.candidate_id \
                 WHERE c.variant_id IN ({placeholders}) \
                 ORDER BY ci.candidate_id"
            );
            let mut statement = connection.prepare(&identity_query)?;
            let rows = statement.query_map(params_from_iter(variant_ids.iter()), |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, Vec<u8>>(1)?))
            })?;
            for row in rows {
                let (candidate_id, key) = row?;
                identities
                    .entry(candidate_id)
                    .or_default()
                    .push(intern_bytes(&mut bytes, key));
            }
        }
        let query = format!(
            "SELECT c.candidate_id, m.mod_id, m.name_display, m.name_key, rv.variant_key, \
                    t.target_key, d.path_key, d.path_display, c.provider_kind, c.archive_key, \
                    c.plugin_key, c.deployable, c.legacy_root, c.legacy_rel, c.flags, \
                    c.destination_id, c.file_id \
             FROM candidates c \
             JOIN route_variants rv ON rv.variant_id=c.variant_id \
             JOIN mods m ON m.mod_id=rv.mod_id \
             JOIN destinations d ON d.destination_id=c.destination_id \
             JOIN targets t ON t.target_id=d.target_id \
             WHERE c.variant_id IN ({placeholders}) \
             ORDER BY c.candidate_id"
        );
        let mut statement = connection.prepare(&query)?;
        let rows = statement.query_map(params_from_iter(variant_ids.iter()), |row| {
            let candidate_id: i64 = row.get(0)?;
            let file_id: i64 = row.get(16)?;
            let raw = raw_by_id
                .get(&file_id)
                .copied()
                .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
            Ok(Candidate {
                id: candidate_id,
                destination_id: row.get(15)?,
                mod_id: row.get(1)?,
                mod_name: intern_str(&mut strings, row.get(2)?),
                mod_key: intern_str(&mut strings, row.get(3)?),
                variant_key: intern_str(&mut strings, row.get(4)?),
                source_rel: raw.source_rel.clone(),
                source_display: raw.source_display.clone(),
                target: intern_str(&mut strings, row.get(5)?),
                destination_key: intern_bytes(&mut bytes, row.get(6)?),
                destination_display: intern_str(&mut strings, row.get(7)?),
                kind: ProviderKind::from_i64(row.get(8)?),
                size: raw.size,
                mtime_ns: raw.mtime_ns,
                ordinal: raw.ordinal,
                identities: identities.remove(&candidate_id).unwrap_or_default(),
                archive_key: row
                    .get::<_, Option<String>>(9)?
                    .map(|value| intern_str(&mut strings, value)),
                plugin_key: row
                    .get::<_, Option<String>>(10)?
                    .map(|value| intern_str(&mut strings, value)),
                deployable: row.get::<_, i64>(11)? != 0,
                legacy_root: row.get::<_, i64>(12)? != 0,
                legacy_rel: intern_str(&mut strings, row.get(13)?),
                flags: row.get::<_, i64>(14)? as u32,
            })
        })?;
        for row in rows {
            candidates.push(row?);
        }
        let candidates = Arc::new(candidates);
        *self.candidate_cache.write() = Some((generation, Arc::new(selected), candidates.clone()));
        Ok(candidates)
    }

    pub fn load_raw_files(&self, intent: &ProfileIntent) -> Result<Arc<Vec<RawCatalogFile>>> {
        let generation = self.inventory_generation.load(Ordering::Acquire);
        let selected = selected_variant_pairs(intent);
        let cached = self.raw_file_cache.read().as_ref().map(
            |(cached_generation, cached_selection, files)| {
                (*cached_generation, cached_selection.clone(), files.clone())
            },
        );
        if let Some((cached_generation, cached_selection, files)) = &cached
            && *cached_generation == generation
            && cached_selection.as_ref() == &selected
        {
            return Ok(files.clone());
        }
        if selected.is_empty() {
            let files = Arc::new(Vec::new());
            *self.raw_file_cache.write() = Some((generation, Arc::new(selected), files.clone()));
            return Ok(files);
        }
        let connection = self.connection()?;
        let selected_keys: HashSet<_> = selected.iter().map(|(key, _)| key.as_str()).collect();
        let mut files = Vec::new();
        let query_selection =
            if let Some((cached_generation, cached_selection, cached_files)) = cached {
                let changed = changed_selection_keys(
                    &connection,
                    cached_generation,
                    &cached_selection,
                    &selected,
                )?;
                files.extend(
                    cached_files
                        .iter()
                        .filter(|file| {
                            selected_keys.contains(file.mod_key.as_ref())
                                && !changed.contains(file.mod_key.as_ref())
                        })
                        .cloned(),
                );
                selected
                    .iter()
                    .filter(|(key, _variant)| changed.contains(key))
                    .cloned()
                    .collect::<Vec<_>>()
            } else {
                selected.clone()
            };
        let mut variant_ids = Vec::with_capacity(query_selection.len());
        {
            let mut find_variant = connection.prepare_cached(
                "SELECT rv.variant_id FROM route_variants rv \
                 JOIN mods m ON m.mod_id=rv.mod_id \
                 WHERE m.name_key=?1 AND rv.variant_key=?2",
            )?;
            for (key, variant) in &query_selection {
                if let Some(variant_id) = find_variant
                    .query_row(params![key, variant], |row| row.get::<_, i64>(0))
                    .optional()?
                {
                    variant_ids.push(variant_id);
                }
            }
        }
        if variant_ids.is_empty() {
            let files = Arc::new(files);
            *self.raw_file_cache.write() = Some((generation, Arc::new(selected), files.clone()));
            return Ok(files);
        }
        let placeholders = std::iter::repeat_n("?", variant_ids.len())
            .collect::<Vec<_>>()
            .join(",");
        let query = format!(
            "SELECT rf.file_id, m.name_display, m.name_key, rf.source_rel, \
                    rf.source_display, rvf.index_display, rf.size, rf.mtime_ns, \
                    rf.ordinal, rvf.flags \
             FROM raw_variant_files rvf \
             JOIN route_variants rv ON rv.variant_id=rvf.variant_id \
             JOIN mods m ON m.mod_id=rv.mod_id \
             JOIN raw_files rf ON rf.file_id=rvf.file_id \
             WHERE rvf.variant_id IN ({placeholders}) \
             ORDER BY m.name_key, rf.ordinal, rf.file_id"
        );
        let mut statement = connection.prepare(&query)?;
        let mut strings: HashSet<Arc<str>> = HashSet::new();
        let mut bytes: HashSet<Arc<[u8]>> = HashSet::new();
        let rows = statement.query_map(params_from_iter(variant_ids), |row| {
            Ok(RawCatalogFile {
                id: row.get(0)?,
                mod_name: intern_str(&mut strings, row.get(1)?),
                mod_key: intern_str(&mut strings, row.get(2)?),
                source_rel: intern_bytes(&mut bytes, row.get(3)?),
                source_display: intern_str(&mut strings, row.get(4)?),
                index_display: intern_str(&mut strings, row.get(5)?),
                size: row.get::<_, i64>(6)? as u64,
                mtime_ns: row.get(7)?,
                ordinal: row.get::<_, i64>(8)? as u32,
                flags: row.get::<_, i64>(9)? as u32,
            })
        })?;
        for row in rows {
            files.push(row?);
        }
        let files = Arc::new(files);
        *self.raw_file_cache.write() = Some((generation, Arc::new(selected), files.clone()));
        Ok(files)
    }

    pub fn persist_profile_delta(
        &self,
        intent: &ProfileIntent,
        previous: &GraphSnapshot,
        update: &GraphUpdate,
    ) -> Result<()> {
        let snapshot = &update.snapshot;
        let mut keeper = self.keeper.lock();
        let connection = keeper
            .as_mut()
            .ok_or_else(|| FileGraphError::Busy("catalog writer is unavailable".to_owned()))?;
        let transaction = connection.transaction()?;
        transaction.execute(
            "INSERT INTO profiles(profile_id, intent_hash, intent_payload, rules_hash, generation, inventory_generation, ready) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, 1) \
             ON CONFLICT(profile_id) DO UPDATE SET intent_hash=excluded.intent_hash, \
               intent_payload=excluded.intent_payload, rules_hash=excluded.rules_hash, generation=excluded.generation, \
               inventory_generation=excluded.inventory_generation, ready=1",
            params![
                intent.profile_id,
                intent.intent_hash,
                rmp_serde::to_vec_named(intent)?,
                intent.rules_hash,
                snapshot.generation as i64,
                snapshot.inventory_generation as i64,
            ],
        )?;

        let (mod_ids, catalog_mods): (HashMap<String, i64>, HashMap<String, i64>) = {
            let mut statement =
                transaction.prepare("SELECT name_display, name_key, mod_id FROM mods")?;
            let rows = statement.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get(2)?,
                ))
            })?;
            let mut by_display = HashMap::new();
            let mut by_key = HashMap::new();
            for row in rows {
                let (display, key, id) = row?;
                by_display.insert(display, id);
                by_key.insert(key, id);
            }
            (by_display, by_key)
        };
        let variant_ids: HashMap<(i64, String), i64> = {
            let mut statement = transaction
                .prepare("SELECT mod_id, variant_key, variant_id FROM route_variants")?;
            let rows =
                statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?;
            let mut result = HashMap::new();
            for row in rows {
                let (mod_id, key, variant_id) = row?;
                result.insert((mod_id, key), variant_id);
            }
            result
        };
        let existing_profile_mods: HashMap<String, PersistedProfileMod> = {
            let mut statement = transaction.prepare(
                "SELECT m.name_key, pm.mod_id, pm.enabled, pm.order_label, pm.variant_id \
                 FROM profile_mods pm JOIN mods m ON m.mod_id=pm.mod_id \
                 WHERE pm.profile_id=?1",
            )?;
            let rows = statement.query_map([&intent.profile_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    PersistedProfileMod {
                        mod_id: row.get(1)?,
                        enabled: row.get::<_, i64>(2)? != 0,
                        order_label: row.get(3)?,
                        variant_id: row.get(4)?,
                    },
                ))
            })?;
            let mut result = HashMap::new();
            for row in rows {
                let (key, value) = row?;
                result.insert(key, value);
            }
            result
        };
        let order_labels = profile_order_labels(intent, &existing_profile_mods);
        let desired_keys: BTreeSet<&str> = intent
            .mods
            .iter()
            .filter(|entry| catalog_mods.contains_key(&entry.key))
            .map(|entry| entry.key.as_str())
            .collect();
        {
            let mut delete_profile_mod = transaction
                .prepare_cached("DELETE FROM profile_mods WHERE profile_id=?1 AND mod_id=?2")?;
            for (key, row) in &existing_profile_mods {
                if desired_keys.contains(key.as_str()) {
                    continue;
                }
                delete_profile_mod.execute(params![intent.profile_id, row.mod_id])?;
            }
        }
        {
            let mut upsert_profile_mod = transaction.prepare_cached(
                "INSERT INTO profile_mods(profile_id, mod_id, enabled, order_label, variant_id) \
                 VALUES(?1, ?2, ?3, ?4, ?5) \
                 ON CONFLICT(profile_id, mod_id) DO UPDATE SET \
                   enabled=excluded.enabled, order_label=excluded.order_label, \
                   variant_id=excluded.variant_id",
            )?;
            for (entry, order_label) in intent.mods.iter().zip(order_labels) {
                let Some(&mod_id) = catalog_mods.get(&entry.key) else {
                    continue;
                };
                let variant_id = variant_ids
                    .get(&(mod_id, entry.variant_key.clone()))
                    .copied();
                let unchanged = existing_profile_mods.get(&entry.key).is_some_and(|old| {
                    old.enabled == entry.enabled
                        && old.order_label == order_label
                        && old.variant_id == variant_id
                });
                if unchanged {
                    continue;
                }
                upsert_profile_mod.execute(params![
                    intent.profile_id,
                    mod_id,
                    entry.enabled as i64,
                    order_label,
                    variant_id,
                ])?;
            }
        }

        {
            let mut upserts = Vec::new();
            let mut deletes = Vec::new();
            for key in &update.changed_winner_keys {
                let old_id = previous.winners.get(key);
                let new_id = snapshot.winners.get(key);
                if old_id == new_id {
                    continue;
                }
                let candidate = new_id
                    .and_then(|candidate_id| snapshot.candidate(*candidate_id))
                    .or_else(|| old_id.and_then(|candidate_id| previous.candidate(*candidate_id)))
                    .ok_or_else(|| {
                        FileGraphError::Corrupt(
                            "changed winner has no catalog candidate".to_owned(),
                        )
                    })?;
                let destination_id = candidate.destination_id;
                let namespace_id = match key.namespace {
                    Namespace::Normal => 0,
                    Namespace::Root => 1,
                    Namespace::Archive => 2,
                };
                if let Some(candidate_id) = new_id {
                    upserts.push((namespace_id, destination_id, *candidate_id));
                } else {
                    deletes.push((namespace_id, destination_id));
                }
            }
            // Three per-row parameters plus two shared values stay below
            // bundled SQLite's 32,766-variable limit at 10k rows. One 10k
            // winner edit therefore requires one prepared statement/step.
            const WINNER_BATCH: usize = 10_000;
            for chunk in upserts.chunks(WINNER_BATCH) {
                let values = (0..chunk.len())
                    .map(|index| {
                        let base = 3 + index * 3;
                        format!("(?1, ?{base}, ?{}, ?{}, ?2)", base + 1, base + 2)
                    })
                    .collect::<Vec<_>>()
                    .join(", ");
                let sql = format!(
                    "INSERT INTO winners(profile_id, namespace, destination_id, candidate_id, generation) \
                     VALUES {values} ON CONFLICT(profile_id, namespace, destination_id) DO UPDATE SET \
                     candidate_id=excluded.candidate_id, generation=excluded.generation"
                );
                let mut parameters = Vec::with_capacity(2 + chunk.len() * 3);
                parameters.push(Value::Text(intent.profile_id.clone()));
                parameters.push(Value::Integer(snapshot.generation as i64));
                for (namespace_id, destination_id, candidate_id) in chunk {
                    parameters.push(Value::Integer(*namespace_id));
                    parameters.push(Value::Integer(*destination_id));
                    parameters.push(Value::Integer(*candidate_id));
                }
                transaction
                    .prepare_cached(&sql)?
                    .execute(params_from_iter(parameters))?;
            }
            for chunk in deletes.chunks(WINNER_BATCH) {
                let values = std::iter::repeat_n("(?, ?)", chunk.len())
                    .collect::<Vec<_>>()
                    .join(", ");
                let sql = format!(
                    "DELETE FROM winners WHERE profile_id=? AND \
                     (namespace, destination_id) IN (VALUES {values})"
                );
                let mut parameters = Vec::with_capacity(1 + chunk.len() * 2);
                parameters.push(Value::Text(intent.profile_id.clone()));
                for (namespace_id, destination_id) in chunk {
                    parameters.push(Value::Integer(*namespace_id));
                    parameters.push(Value::Integer(*destination_id));
                }
                transaction
                    .prepare_cached(&sql)?
                    .execute(params_from_iter(parameters))?;
            }
        }

        {
            let mut delete_edge = transaction.prepare_cached(
                "DELETE FROM conflict_edges WHERE profile_id=?1 AND conflict_kind=?2 \
                 AND loser_mod_id=?3 AND winner_mod_id=?4",
            )?;
            let mut upsert_edge = transaction.prepare_cached(
                "INSERT INTO conflict_edges(profile_id, conflict_kind, loser_mod_id, winner_mod_id, refcount, generation) \
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6) \
                 ON CONFLICT(profile_id, conflict_kind, loser_mod_id, winner_mod_id) \
                 DO UPDATE SET refcount=excluded.refcount, generation=excluded.generation",
            )?;
            for edge in &update.changed_edge_keys {
                let old_refcount = previous.edges.get(edge);
                let new_refcount = snapshot.edges.get(edge);
                if old_refcount == new_refcount {
                    continue;
                }
                if let Some(refcount) = new_refcount {
                    upsert_edge.execute(params![
                        intent.profile_id,
                        edge.kind.as_str(),
                        edge.loser,
                        edge.winner,
                        *refcount as i64,
                        snapshot.generation as i64,
                    ])?;
                } else {
                    delete_edge.execute(params![
                        intent.profile_id,
                        edge.kind.as_str(),
                        edge.loser,
                        edge.winner,
                    ])?;
                }
            }
        }

        {
            let mut delete_summary = transaction
                .prepare_cached("DELETE FROM mod_summaries WHERE profile_id=?1 AND mod_id=?2")?;
            let mut upsert_summary = transaction.prepare_cached(
                "INSERT INTO mod_summaries(profile_id, mod_id, payload, generation) \
                 VALUES(?1, ?2, ?3, ?4) \
                 ON CONFLICT(profile_id, mod_id) DO UPDATE SET \
                   payload=excluded.payload, generation=excluded.generation",
            )?;
            for name in &update.changed_summary_names {
                let old_summary = previous.summaries.get(name);
                let new_summary = snapshot.summaries.get(name);
                if old_summary == new_summary {
                    continue;
                }
                let Some(mod_id) = mod_ids.get(name) else {
                    continue;
                };
                if let Some(summary) = new_summary {
                    upsert_summary.execute(params![
                        intent.profile_id,
                        mod_id,
                        rmp_serde::to_vec_named(summary)?,
                        snapshot.generation as i64,
                    ])?;
                } else {
                    delete_summary.execute(params![intent.profile_id, mod_id])?;
                }
            }
        }
        transaction.commit()?;
        Ok(())
    }
}

pub struct ProfileCore {
    pub library: Arc<LibraryCore>,
    pub profile_id: String,
    pub state: RwLock<ProfileState>,
    prepared_deployment_plan: Mutex<Option<Arc<DeploymentPlanRecord>>>,
}

pub struct ProfileState {
    pub intent: Option<ProfileIntent>,
    pub snapshot: Arc<GraphSnapshot>,
}

impl ProfileCore {
    pub fn new(library: Arc<LibraryCore>, profile_id: String) -> Result<Arc<Self>> {
        let connection = library.connection()?;
        let inventory_generation = library.inventory_generation.load(Ordering::Acquire);
        let restored: Option<(Vec<u8>, u64, u64)> = connection
            .query_row(
                "SELECT intent_payload, generation, inventory_generation \
                 FROM profiles WHERE profile_id=?1 AND ready=1",
                [&profile_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get::<_, i64>(1)? as u64,
                        row.get::<_, i64>(2)? as u64,
                    ))
                },
            )
            .optional()?;
        drop(connection);
        let (intent, snapshot) = match restored {
            Some((payload, generation, stored_inventory))
                if !payload.is_empty() && stored_inventory == inventory_generation =>
            {
                match rmp_serde::from_slice::<ProfileIntent>(&payload) {
                    Ok(intent) => {
                        let restore_started = Instant::now();
                        let candidates = library.load_candidates(&intent)?;
                        let candidates_elapsed = restore_started.elapsed();
                        let raw_started = Instant::now();
                        let raw_files = library.load_raw_files(&intent)?;
                        let raw_elapsed = raw_started.elapsed();
                        let graph_started = Instant::now();
                        let snapshot = crate::graph::build_full(
                            candidates,
                            raw_files,
                            &intent,
                            inventory_generation,
                            generation,
                        );
                        if std::env::var_os("AMETHYST_FILEGRAPH_TRACE").is_some() {
                            eprintln!(
                                "[filegraph] restore profile={} candidates={} raw_files={} \
                                 load_candidates_ms={:.3} cached_raw_ms={:.3} graph_ms={:.3}",
                                profile_id,
                                snapshot.candidates.len(),
                                snapshot.raw_file_count(),
                                candidates_elapsed.as_secs_f64() * 1_000.0,
                                raw_elapsed.as_secs_f64() * 1_000.0,
                                graph_started.elapsed().as_secs_f64() * 1_000.0,
                            );
                        }
                        (Some(intent), Arc::new(snapshot))
                    }
                    Err(_) => (None, Arc::new(GraphSnapshot::empty(inventory_generation))),
                }
            }
            _ => (None, Arc::new(GraphSnapshot::empty(inventory_generation))),
        };
        Ok(Arc::new(Self {
            library,
            profile_id,
            state: RwLock::new(ProfileState { intent, snapshot }),
            prepared_deployment_plan: Mutex::new(None),
        }))
    }

    pub fn reconcile(
        &self,
        intent: ProfileIntent,
        cancelled: &AtomicBool,
    ) -> Result<ResolutionDelta> {
        if intent.profile_id != self.profile_id {
            return Err(FileGraphError::Invalid(format!(
                "intent profile_id {:?} does not match session {:?}",
                intent.profile_id, self.profile_id
            )));
        }
        if cancelled.load(Ordering::Relaxed) {
            return Err(FileGraphError::Invalid("operation cancelled".to_owned()));
        }
        self.ensure_no_active_deployment()?;
        let reconcile_started = Instant::now();
        let inventory_generation = self.library.inventory_generation.load(Ordering::Acquire);
        let candidates = self.library.load_candidates(&intent)?;
        let candidates_elapsed = reconcile_started.elapsed();
        let raw_files = self.library.load_raw_files(&intent)?;
        let raw_elapsed = reconcile_started.elapsed();
        let (previous, previous_intent) = {
            let state = self.state.read();
            (state.snapshot.clone(), state.intent.clone())
        };
        let generation = previous.generation.saturating_add(1);
        let graph_started = Instant::now();
        let mut update = reconcile_graph(
            &previous,
            previous_intent.as_ref(),
            candidates,
            raw_files,
            &intent,
            inventory_generation,
            generation,
        );
        let graph_elapsed = graph_started.elapsed();
        update.delta.graph_compute_ns = graph_elapsed.as_nanos() as u64;
        if cancelled.load(Ordering::Relaxed) {
            return Err(FileGraphError::Invalid("operation cancelled".to_owned()));
        }
        let persist_started = Instant::now();
        self.library
            .persist_profile_delta(&intent, &previous, &update)?;
        let persist_elapsed = persist_started.elapsed();
        update.delta.sqlite_commit_ns = persist_elapsed.as_nanos() as u64;
        let delta = update.delta;
        let snapshot = Arc::new(update.snapshot);
        let mut state = self.state.write();
        state.intent = Some(intent);
        state.snapshot = snapshot;
        drop(state);
        // Keep the old generation's disposable plan until another plan
        // replaces it.  deployment_plan() validates the generation before it
        // can be returned, so retaining one stale Arc is safe and bounded.
        // Eagerly dropping 100k+ DeployEntryRecords here made an otherwise
        // tiny toggle spend well over 100 ms freeing deployment-only data on
        // the conflict worker. Replacement happens while Deploy builds the
        // next generation instead of on the interactive reconcile path.
        if crate::model::perftrace_enabled() {
            eprintln!(
                "[FILEGRAPH-TIMING] reconcile: [DB I/O + CPU] candidates {:.3}s, \
                 [DB I/O + CPU] raw {:.3}s, [CPU] graph {:.3}s, \
                 [DB I/O] persist {:.3}s, total {:.3}s",
                candidates_elapsed.as_secs_f64(),
                (raw_elapsed - candidates_elapsed).as_secs_f64(),
                graph_elapsed.as_secs_f64(),
                persist_elapsed.as_secs_f64(),
                reconcile_started.elapsed().as_secs_f64(),
            );
        }
        Ok(delta)
    }

    /// Return one generation-pinned deployment plan, retaining the expensive
    /// sorted projection for Python export, journalling, and commit.
    pub fn deployment_plan(&self, generation: u64) -> Result<Arc<DeploymentPlanRecord>> {
        if let Some(plan) = self.prepared_deployment_plan.lock().as_ref() {
            if plan.generation == generation {
                return Ok(plan.clone());
            }
        }
        let snapshot = self.snapshot();
        if snapshot.generation != generation {
            return Err(FileGraphError::Invalid(format!(
                "snapshot generation {generation} is stale; current generation is {}",
                snapshot.generation
            )));
        }
        let plan = Arc::new(snapshot.deployment_plan());
        // A reconcile may have published while the plan was being sorted.
        // Never install an old generation into the disposable cache.
        let current_generation = self.snapshot().generation;
        if current_generation != generation {
            return Err(FileGraphError::Invalid(format!(
                "prepared deployment generation {generation} was superseded by generation {current_generation}"
            )));
        }
        *self.prepared_deployment_plan.lock() = Some(plan.clone());
        Ok(plan)
    }

    fn ensure_no_active_deployment(&self) -> Result<()> {
        self.library
            .ensure_profile_no_active_operations(&self.profile_id)
    }

    pub fn begin_deployment(
        &self,
        operation_id: &str,
        generation: u64,
        link_mode: &str,
    ) -> Result<DeploymentPlanRecord> {
        if operation_id.trim().is_empty() || link_mode.trim().is_empty() {
            return Err(FileGraphError::Invalid(
                "deployment operation id and link mode are required".to_owned(),
            ));
        }
        self.ensure_no_active_deployment()?;
        let plan = self.deployment_plan(generation)?;
        // The current deployed state already remains intact in
        // `deployed_entries` until commit, and the pinned winners remain in the
        // profile generation.  Duplicating both complete 80k-entry sets into
        // one journal made begin-deploy serialize/write 60+ MiB before a single
        // filesystem operation.  Recovery only needs the pinned generation;
        // old journals with embedded entries remain readable.
        let journal = DeploymentJournal {
            generation: plan.generation,
            inventory_generation: plan.inventory_generation,
            link_mode: link_mode.to_owned(),
            phase: "planned".to_owned(),
            previous_entries: Vec::new(),
            entries: Vec::new(),
        };
        let timestamp = now_ns();
        let connection = self.library.connection()?;
        connection.execute(
            "INSERT INTO operations(operation_id, profile_id, kind, state, phase, payload, created_ns, updated_ns) \
             VALUES(?1, ?2, 'deployment', 'planned', 'planned', ?3, ?4, ?4)",
            params![
                operation_id,
                self.profile_id,
                // Internal recovery data does not need field names repeated
                // for every entry. from_slice accepts old named journals too.
                rmp_serde::to_vec(&journal)?,
                timestamp as i64,
            ],
        )?;
        Ok((*plan).clone())
    }

    /// Start a deployment journal without materialising the winner plan.
    ///
    /// Python may already hold an immutable plan prepared while the UI was
    /// idle.  Rebuilding and serialising every entry here made the Deploy
    /// button pay for the same generation twice.  The journal only needs the
    /// pinned generation/inventory identity; commit reconstructs from that
    /// immutable native snapshot exactly as the compact journal path above
    /// already does.
    pub fn begin_prepared_deployment(
        &self,
        operation_id: &str,
        generation: u64,
        link_mode: &str,
    ) -> Result<()> {
        if operation_id.trim().is_empty() || link_mode.trim().is_empty() {
            return Err(FileGraphError::Invalid(
                "deployment operation id and link mode are required".to_owned(),
            ));
        }
        self.ensure_no_active_deployment()?;
        let snapshot = self.snapshot();
        if snapshot.generation != generation {
            return Err(FileGraphError::Invalid(format!(
                "snapshot generation {generation} is stale; current generation is {}",
                snapshot.generation
            )));
        }
        let journal = DeploymentJournal {
            generation: snapshot.generation,
            inventory_generation: snapshot.inventory_generation,
            link_mode: link_mode.to_owned(),
            phase: "planned".to_owned(),
            previous_entries: Vec::new(),
            entries: Vec::new(),
        };
        let timestamp = now_ns();
        let connection = self.library.connection()?;
        connection.execute(
            "INSERT INTO operations(operation_id, profile_id, kind, state, phase, payload, created_ns, updated_ns) \
             VALUES(?1, ?2, 'deployment', 'planned', 'planned', ?3, ?4, ?4)",
            params![
                operation_id,
                self.profile_id,
                rmp_serde::to_vec(&journal)?,
                timestamp as i64,
            ],
        )?;
        Ok(())
    }

    pub fn deployment_unchanged(&self, generation: u64, link_mode: &str) -> Result<bool> {
        self.ensure_no_active_deployment()?;
        let snapshot = self.snapshot();
        if snapshot.generation != generation {
            return Err(FileGraphError::Invalid(format!(
                "snapshot generation {generation} is stale; current generation is {}",
                snapshot.generation
            )));
        }
        let previous = self.deployed_entries()?;
        Ok(snapshot.deployment_matches(&previous, link_mode))
    }

    pub fn update_deployment_phase(&self, operation_id: &str, phase: &str) -> Result<()> {
        const PHASES: &[&str] = &[
            "planned",
            "removing",
            "backing_up",
            "placing",
            "post_deploy",
            "database_commit",
        ];
        if !PHASES.contains(&phase) {
            return Err(FileGraphError::Invalid(format!(
                "unknown deployment journal phase {phase:?}"
            )));
        }
        let connection = self.library.connection()?;
        let (profile_id, state): (String, String) = connection
            .query_row(
                "SELECT profile_id, state FROM operations \
                 WHERE operation_id=?1 AND kind='deployment'",
                [operation_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?
            .ok_or_else(|| {
                FileGraphError::Invalid(format!(
                    "deployment operation {operation_id:?} does not exist"
                ))
            })?;
        if profile_id != self.profile_id || !matches!(state.as_str(), "planned" | "mutating") {
            return Err(FileGraphError::Invalid(format!(
                "deployment operation {operation_id:?} cannot advance from state {state:?}"
            )));
        }
        let next_state = if phase == "planned" {
            "planned"
        } else {
            "mutating"
        };
        connection.execute(
            "UPDATE operations SET state=?2, phase=?3, updated_ns=?4 \
             WHERE operation_id=?1",
            params![operation_id, next_state, phase, now_ns() as i64,],
        )?;
        Ok(())
    }

    pub fn commit_deployment(&self, operation_id: &str) -> Result<()> {
        let commit_started = Instant::now();
        let mut connection = self.library.connection()?;
        let transaction = connection.transaction()?;
        let (profile_id, state, payload): (String, String, Vec<u8>) = transaction
            .query_row(
                "SELECT profile_id, state, payload FROM operations \
                 WHERE operation_id=?1 AND kind='deployment'",
                [operation_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?
            .ok_or_else(|| {
                FileGraphError::Invalid(format!(
                    "deployment operation {operation_id:?} does not exist"
                ))
            })?;
        if profile_id != self.profile_id || !matches!(state.as_str(), "planned" | "mutating") {
            return Err(FileGraphError::Invalid(format!(
                "deployment operation {operation_id:?} cannot be committed from state {state:?}"
            )));
        }
        let journal: DeploymentJournal = rmp_serde::from_slice(&payload)?;
        let current_generation: u64 = transaction.query_row(
            "SELECT generation FROM profiles WHERE profile_id=?1",
            [&self.profile_id],
            |row| Ok(row.get::<_, i64>(0)? as u64),
        )?;
        if current_generation != journal.generation {
            return Err(FileGraphError::Invalid(format!(
                "deployment generation {} is stale; profile is at {current_generation}",
                journal.generation
            )));
        }
        let prepared_plan;
        let legacy_entries;
        let entries = if journal.entries.is_empty() {
            prepared_plan = self.deployment_plan(journal.generation)?;
            let plan = prepared_plan.as_ref();
            if plan.generation != journal.generation
                || plan.inventory_generation != journal.inventory_generation
            {
                return Err(FileGraphError::Invalid(format!(
                    "deployment journal generation {} no longer has a matching pinned plan",
                    journal.generation
                )));
            }
            plan.entries.as_slice()
        } else {
            // Compatibility with schema 4-6 journals written before compact
            // generation-pinned deployment records.
            legacy_entries = journal.entries;
            legacy_entries.as_slice()
        };
        let plan_elapsed = commit_started.elapsed();
        // Read the prior successful state once, then mutate only destinations
        // whose deployed identity changed.  The previous implementation
        // deleted and reinserted every row, making a one-mod redeploy pay the
        // SQLite cost of the entire 80k+ winner set.
        let mut previous_by_target: HashMap<String, HashMap<Vec<u8>, DeployedStateRecord>> =
            HashMap::new();
        {
            let mut statement = transaction.prepare(
                "SELECT target_key, destination_key, destination_display, candidate_id, \
                        mod_name, mod_key, provider_kind, source_rel, source_display, \
                        source_fingerprint, link_mode, deployed_generation \
                 FROM deployed_entries WHERE profile_id=?1",
            )?;
            let rows = statement.query_map([&self.profile_id], |row| {
                Ok(DeployedStateRecord {
                    target: row.get(0)?,
                    destination_key: row.get(1)?,
                    destination_display: row.get(2)?,
                    candidate_id: row.get(3)?,
                    mod_name: row.get(4)?,
                    mod_key: row.get(5)?,
                    provider_kind: ProviderKind::from_i64(row.get(6)?),
                    source_rel: row.get(7)?,
                    source_display: row.get(8)?,
                    source_fingerprint: row.get(9)?,
                    link_mode: row.get(10)?,
                    deployed_generation: row.get::<_, i64>(11)? as u64,
                })
            })?;
            for row in rows {
                let deployed = row?;
                previous_by_target
                    .entry(deployed.target.clone())
                    .or_default()
                    .insert(deployed.destination_key.clone(), deployed);
            }
        }
        let read_elapsed = commit_started.elapsed();

        let mut changed_entries = Vec::new();
        for entry in entries {
            let previous = previous_by_target
                .get_mut(entry.target.as_str())
                .and_then(|destinations| destinations.remove(entry.destination_key.as_slice()));
            let unchanged = previous.as_ref().is_some_and(|deployed| {
                deployed.destination_display == entry.destination_display
                    && deployed.candidate_id == entry.candidate_id
                    && deployed.mod_name == entry.mod_name
                    && deployed.mod_key == entry.mod_key
                    && deployed.provider_kind == entry.provider_kind
                    && deployed.source_rel == entry.source_rel
                    && deployed.source_display == entry.source_display
                    && deployed.source_fingerprint == entry.source_fingerprint
                    && deployed.link_mode.eq_ignore_ascii_case(&journal.link_mode)
            });
            if !unchanged {
                // Most redeploys change a small fraction of the winner set.
                // Clone only rows which SQLite will actually upsert instead
                // of cloning the complete retained native plan.
                changed_entries.push(entry.clone());
            }
        }
        let diff_elapsed = commit_started.elapsed();
        let removed_count: usize = previous_by_target.values().map(HashMap::len).sum();
        let changed_count = changed_entries.len();

        {
            let mut delete_entry = transaction.prepare_cached(
                "DELETE FROM deployed_entries \
                 WHERE profile_id=?1 AND target_key=?2 AND destination_key=?3",
            )?;
            for (target, destinations) in previous_by_target {
                for (destination_key, _entry) in destinations {
                    delete_entry.execute(params![self.profile_id, target, destination_key,])?;
                }
            }
        }
        {
            let mut upsert_entry = transaction.prepare_cached(
                "INSERT INTO deployed_entries(
                    profile_id, target_key, destination_key, destination_display,
                    candidate_id, mod_name, mod_key, provider_kind, source_rel,
                    source_display, source_fingerprint, link_mode,
                    deployed_generation)
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
                 ON CONFLICT(profile_id, target_key, destination_key) DO UPDATE SET
                    destination_display=excluded.destination_display,
                    candidate_id=excluded.candidate_id,
                    mod_name=excluded.mod_name,
                    mod_key=excluded.mod_key,
                    provider_kind=excluded.provider_kind,
                    source_rel=excluded.source_rel,
                    source_display=excluded.source_display,
                    source_fingerprint=excluded.source_fingerprint,
                    link_mode=excluded.link_mode,
                    deployed_generation=excluded.deployed_generation",
            )?;
            for entry in changed_entries {
                upsert_entry.execute(params![
                    self.profile_id,
                    entry.target,
                    entry.destination_key,
                    entry.destination_display,
                    entry.candidate_id,
                    entry.mod_name,
                    entry.mod_key,
                    entry.provider_kind.as_i64(),
                    entry.source_rel,
                    entry.source_display,
                    entry.source_fingerprint,
                    journal.link_mode,
                    journal.generation as i64,
                ])?;
            }
        }
        transaction.execute(
            "DELETE FROM operations WHERE operation_id=?1",
            [operation_id],
        )?;
        transaction.commit()?;
        let total_elapsed = commit_started.elapsed();
        if crate::model::perftrace_enabled() {
            eprintln!(
                "[FILEGRAPH-TIMING] deploy commit: [CPU] plan {:.3}s, \
                 [DB I/O] deployed-row read {:.3}s, [CPU] diff {:.3}s, \
                 [DB I/O] SQLite delete {} + upsert {} {:.3}s, total {:.3}s",
                plan_elapsed.as_secs_f64(),
                (read_elapsed - plan_elapsed).as_secs_f64(),
                (diff_elapsed - read_elapsed).as_secs_f64(),
                removed_count,
                changed_count,
                (total_elapsed - diff_elapsed).as_secs_f64(),
                total_elapsed.as_secs_f64(),
            );
        }
        Ok(())
    }

    pub fn fail_deployment(&self, operation_id: &str) -> Result<()> {
        let connection = self.library.connection()?;
        let changed = connection.execute(
            "UPDATE operations SET state='failed', payload=X'', updated_ns=?3 \
             WHERE operation_id=?1 AND profile_id=?2 AND kind='deployment' \
             AND state IN ('planned', 'mutating')",
            params![operation_id, self.profile_id, now_ns() as i64],
        )?;
        if changed == 0 {
            return Err(FileGraphError::Invalid(format!(
                "active deployment operation {operation_id:?} does not exist"
            )));
        }
        Ok(())
    }

    pub fn incomplete_operations(&self) -> Result<Vec<OperationRecord>> {
        let connection = self.library.connection()?;
        let mut statement = connection.prepare(
            "SELECT operation_id, profile_id, kind, state, phase, payload, created_ns, updated_ns \
             FROM operations WHERE profile_id=?1 AND state IN ('planned', 'mutating') \
             ORDER BY created_ns",
        )?;
        let rows = statement.query_map([&self.profile_id], |row| {
            let phase: String = row.get(4)?;
            let payload: Vec<u8> = row.get(5)?;
            let generation = rmp_serde::from_slice::<DeploymentJournal>(&payload)
                .map(|journal| journal.generation)
                .unwrap_or(0);
            Ok(OperationRecord {
                operation_id: row.get(0)?,
                profile_id: row.get(1)?,
                kind: row.get(2)?,
                state: row.get(3)?,
                phase,
                generation,
                created_ns: row.get::<_, i64>(6)? as u64,
                updated_ns: row.get::<_, i64>(7)? as u64,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    pub fn deployed_entries(&self) -> Result<Vec<DeployedStateRecord>> {
        let connection = self.library.connection()?;
        let mut statement = connection.prepare(
            "SELECT target_key, destination_key, destination_display, candidate_id, \
                    mod_name, mod_key, provider_kind, source_rel, source_display, \
                    source_fingerprint, link_mode, deployed_generation \
             FROM deployed_entries WHERE profile_id=?1 \
             ORDER BY target_key, destination_key",
        )?;
        let rows = statement.query_map([&self.profile_id], |row| {
            Ok(DeployedStateRecord {
                target: row.get(0)?,
                destination_key: row.get(1)?,
                destination_display: row.get(2)?,
                candidate_id: row.get(3)?,
                mod_name: row.get(4)?,
                mod_key: row.get(5)?,
                provider_kind: ProviderKind::from_i64(row.get(6)?),
                source_rel: row.get(7)?,
                source_display: row.get(8)?,
                source_fingerprint: row.get(9)?,
                link_mode: row.get(10)?,
                deployed_generation: row.get::<_, i64>(11)? as u64,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    pub fn forget_deployed_mods(&self, mod_keys: &[String]) -> Result<u64> {
        if mod_keys.is_empty() {
            return Ok(0);
        }
        let mut connection = self.library.connection()?;
        let transaction = connection.transaction()?;
        let mut removed = 0_u64;
        for key in mod_keys {
            removed += transaction.execute(
                "DELETE FROM deployed_entries WHERE profile_id=?1 AND mod_key=?2",
                params![self.profile_id, key],
            )? as u64;
        }
        transaction.commit()?;
        Ok(removed)
    }

    pub fn snapshot(&self) -> Arc<GraphSnapshot> {
        self.state.read().snapshot.clone()
    }
}

pub fn database_name(root: &Path) -> PathBuf {
    root.join("filegraph.sqlite3")
}
