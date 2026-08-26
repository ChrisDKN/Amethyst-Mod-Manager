use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

pub const API_VERSION: u32 = 10;
pub const SCHEMA_VERSION: u32 = 9;
pub const ENGINE_REVISION: u64 = 1;
pub const RULES_REVISION: u64 = 7;

pub fn perftrace_enabled() -> bool {
    std::env::var_os("MM_PERFTRACE").is_some_and(|value| {
        let value = value.to_string_lossy();
        !matches!(value.as_ref(), "" | "0" | "false" | "False")
    })
}

fn default_true() -> bool {
    true
}

#[derive(
    Clone, Copy, Debug, Default, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize,
)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    #[default]
    Loose,
    Root,
    Overwrite,
    ArchiveMember,
}

impl ProviderKind {
    pub fn as_i64(self) -> i64 {
        match self {
            Self::Loose => 0,
            Self::Root => 1,
            Self::Overwrite => 2,
            Self::ArchiveMember => 3,
        }
    }

    pub fn from_i64(value: i64) -> Self {
        match value {
            1 => Self::Root,
            2 => Self::Overwrite,
            3 => Self::ArchiveMember,
            _ => Self::Loose,
        }
    }

    pub fn namespace(self) -> Namespace {
        match self {
            Self::Root => Namespace::Root,
            Self::ArchiveMember => Namespace::Archive,
            Self::Loose | Self::Overwrite => Namespace::Normal,
        }
    }
}

#[derive(
    Clone, Copy, Debug, Default, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize,
)]
#[serde(rename_all = "snake_case")]
pub enum Namespace {
    #[default]
    Normal,
    Root,
    Archive,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CandidateRecord {
    pub source_rel: Vec<u8>,
    pub source_display: String,
    pub target: String,
    pub destination_key: Vec<u8>,
    pub destination_display: String,
    #[serde(default)]
    pub kind: ProviderKind,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub mtime_ns: i64,
    #[serde(default)]
    pub ordinal: u32,
    #[serde(default)]
    pub identities: Vec<Vec<u8>>,
    #[serde(default)]
    pub archive_key: Option<String>,
    #[serde(default)]
    pub plugin_key: Option<String>,
    #[serde(default = "default_true")]
    pub deployable: bool,
    #[serde(default)]
    pub legacy_root: bool,
    #[serde(default)]
    pub legacy_rel: String,
    #[serde(default)]
    pub flags: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RawFileRecord {
    pub source_rel: Vec<u8>,
    pub source_display: String,
    #[serde(default)]
    pub index_display: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub mtime_ns: i64,
    #[serde(default)]
    pub ordinal: u32,
    #[serde(default)]
    pub flags: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ManifestBatch {
    pub mod_name: String,
    pub mod_key: String,
    pub variant_key: String,
    #[serde(default)]
    pub manifest_fingerprint: Vec<u8>,
    #[serde(default)]
    pub raw_files: Vec<RawFileRecord>,
    pub candidates: Vec<CandidateRecord>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IntentMod {
    pub name: String,
    pub key: String,
    pub enabled: bool,
    #[serde(default)]
    pub variant_key: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct OperationHint {
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub mods: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProfileIntent {
    pub profile_id: String,
    pub intent_hash: Vec<u8>,
    pub rules_hash: Vec<u8>,
    pub mods: Vec<IntentMod>,
    #[serde(default)]
    pub special_variants: BTreeMap<String, String>,
    #[serde(default)]
    pub archive_order: Vec<String>,
    #[serde(default)]
    pub plugin_order: Vec<String>,
    #[serde(default)]
    pub plugin_extensions: Vec<String>,
    #[serde(default)]
    pub disabled_plugin_paths: BTreeMap<String, BTreeSet<Vec<u8>>>,
    #[serde(default = "default_true")]
    pub loose_beats_archive: bool,
    #[serde(default)]
    pub normalize_folder_case: bool,
    #[serde(default = "default_casing_strategy")]
    pub casing_strategy: String,
    #[serde(default)]
    pub casing_pins: BTreeMap<String, String>,
    #[serde(default)]
    pub hint: OperationHint,
}

fn default_casing_strategy() -> String {
    "upper".to_owned()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub id: i64,
    pub destination_id: i64,
    pub mod_id: i64,
    pub mod_name: Arc<str>,
    pub mod_key: Arc<str>,
    pub variant_key: Arc<str>,
    pub source_rel: Arc<[u8]>,
    pub source_display: Arc<str>,
    pub target: Arc<str>,
    pub destination_key: Arc<[u8]>,
    pub destination_display: Arc<str>,
    pub kind: ProviderKind,
    pub size: u64,
    pub mtime_ns: i64,
    pub ordinal: u32,
    pub identities: Vec<Arc<[u8]>>,
    pub archive_key: Option<Arc<str>>,
    pub plugin_key: Option<Arc<str>>,
    pub deployable: bool,
    pub legacy_root: bool,
    pub legacy_rel: Arc<str>,
    pub flags: u32,
}

#[derive(Clone, Debug)]
pub struct RawCatalogFile {
    pub id: i64,
    pub mod_name: Arc<str>,
    pub mod_key: Arc<str>,
    pub source_rel: Arc<[u8]>,
    pub source_display: Arc<str>,
    pub index_display: Arc<str>,
    pub size: u64,
    pub mtime_ns: i64,
    pub ordinal: u32,
    pub flags: u32,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
pub struct ConflictSummary {
    pub loose_code: i8,
    pub archive_code: i8,
    pub identity_code: i8,
    pub loose_wins: u64,
    pub loose_losses: u64,
    pub loose_surviving: u64,
    pub archive_wins: u64,
    pub archive_losses: u64,
    pub archive_surviving: u64,
    pub identity_wins: u64,
    pub identity_losses: u64,
    #[serde(default)]
    pub flags: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct WinnerRecord {
    pub candidate_id: i64,
    pub mod_name: String,
    pub mod_key: String,
    pub target: String,
    pub destination_key: Vec<u8>,
    pub destination_display: String,
    pub source_rel: Vec<u8>,
    pub source_display: String,
    pub namespace: Namespace,
    pub legacy_root: bool,
    pub legacy_rel: String,
    pub flags: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ProviderRecord {
    pub candidate_id: i64,
    pub mod_name: String,
    pub kind: ProviderKind,
    pub winning: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ModFileRecord {
    pub candidate_id: i64,
    pub mod_name: String,
    pub source_rel: Vec<u8>,
    pub source_display: String,
    pub target: String,
    pub destination_key: Vec<u8>,
    pub destination_display: String,
    pub namespace: Namespace,
    pub provider_kind: ProviderKind,
    pub enabled: bool,
    pub winning: bool,
    pub conflict_status: i8,
    pub deployable: bool,
    pub flags: u32,
    pub plugin_key: Option<String>,
    pub legacy_rel: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct ConflictEdgeRecord {
    pub kind: String,
    pub loser: String,
    pub winner: String,
    pub refcount: u64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ResolutionDelta {
    pub base_generation: u64,
    pub generation: u64,
    pub inventory_generation: u64,
    pub full_rebuild: bool,
    pub candidates_touched: u64,
    pub destinations_touched: u64,
    #[serde(default)]
    pub graph_compute_ns: u64,
    #[serde(default)]
    pub sqlite_commit_ns: u64,
    pub changed_winner_ids: Vec<i64>,
    pub removed_winner_ids: Vec<i64>,
    /// Current winners on every destination whose provider stack was touched,
    /// including paths where the winner stayed the same but contention changed.
    #[serde(default)]
    pub touched_winner_ids: Vec<i64>,
    pub changed_summaries: BTreeMap<String, ConflictSummary>,
    pub changed_plugin_owners: BTreeMap<String, Option<String>>,
    /// Current aggregate raw-file flags for mods whose capabilities changed.
    /// A missing mod is represented by `None` so incremental consumers can
    /// remove stale presentation and fast-path capability state.
    #[serde(default)]
    pub changed_capability_flags: BTreeMap<String, Option<u32>>,
    pub changed_edges: Vec<ConflictEdgeRecord>,
    pub deployment_dirty: bool,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct CatalogStatus {
    pub schema_version: u32,
    pub api_version: u32,
    pub inventory_generation: u64,
    pub engine_revision: u64,
    pub rules_revision: u64,
    pub ready: bool,
    pub mod_count: u64,
    pub candidate_count: u64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SnapshotExport {
    pub generation: u64,
    pub inventory_generation: u64,
    pub winners: Vec<WinnerRecord>,
    pub summaries: BTreeMap<String, ConflictSummary>,
    pub edges: Vec<ConflictEdgeRecord>,
    pub plugin_owners: BTreeMap<String, String>,
    pub capability_flags: BTreeMap<String, u32>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct ConflictStateExport {
    pub generation: u64,
    pub summaries: BTreeMap<String, ConflictSummary>,
    pub edges: Vec<ConflictEdgeRecord>,
    pub plugin_owners: BTreeMap<String, String>,
    pub archive_plugin_stems: BTreeMap<String, BTreeSet<String>>,
    pub capability_flags: BTreeMap<String, u32>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct InventoryFacets {
    pub filetype_counts: BTreeMap<String, u64>,
    pub mod_filetypes: BTreeMap<String, BTreeSet<String>>,
    pub mods_with_pbr: BTreeSet<String>,
    pub mods_with_plugins: BTreeSet<String>,
    pub mods_with_archives: BTreeSet<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct DeployEntryRecord {
    pub candidate_id: i64,
    pub mod_name: String,
    pub mod_key: String,
    pub provider_kind: ProviderKind,
    pub target: String,
    pub destination_key: Vec<u8>,
    pub destination_display: String,
    pub source_rel: Vec<u8>,
    pub source_display: String,
    pub source_fingerprint: Vec<u8>,
    pub legacy_root: bool,
    pub legacy_rel: String,
    pub flags: u32,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
pub struct DeploymentPlanRecord {
    pub generation: u64,
    pub inventory_generation: u64,
    pub entries: Vec<DeployEntryRecord>,
}

#[derive(Clone, Debug, Serialize)]
pub struct DataEntryRecord {
    pub candidate_id: i64,
    pub mod_name: String,
    pub target: String,
    pub destination_display: String,
    pub contested: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct DeploymentJournal {
    pub generation: u64,
    pub inventory_generation: u64,
    pub link_mode: String,
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub previous_entries: Vec<DeployedStateRecord>,
    #[serde(default)]
    pub entries: Vec<DeployEntryRecord>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct OperationRecord {
    pub operation_id: String,
    pub profile_id: String,
    pub kind: String,
    pub state: String,
    pub phase: String,
    pub generation: u64,
    pub created_ns: u64,
    pub updated_ns: u64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct DeployedStateRecord {
    pub target: String,
    pub destination_key: Vec<u8>,
    pub destination_display: String,
    pub candidate_id: i64,
    pub mod_name: String,
    pub mod_key: String,
    pub provider_kind: ProviderKind,
    pub source_rel: Vec<u8>,
    pub source_display: String,
    pub source_fingerprint: Vec<u8>,
    pub link_mode: String,
    pub deployed_generation: u64,
}
