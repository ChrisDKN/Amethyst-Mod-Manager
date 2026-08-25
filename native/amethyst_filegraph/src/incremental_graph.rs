use crate::model::{
    Candidate, ConflictEdgeRecord, ConflictStateExport, ConflictSummary, DataEntryRecord,
    DeployEntryRecord, DeployedStateRecord, DeploymentPlanRecord, InventoryFacets, ModFileRecord,
    Namespace, ProfileIntent, ProviderKind, ProviderRecord, RawCatalogFile, ResolutionDelta,
    SnapshotExport, WinnerRecord,
};
use im::{HashMap as PersistentHashMap, HashSet as PersistentHashSet};
use smallvec::SmallVec;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct PathKey {
    pub(crate) namespace: Namespace,
    pub(crate) effective: u32,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct EffectivePath {
    target: Arc<str>,
    path: Arc<[u8]>,
}

type EffectiveKey = u32;
type ProviderIndex = u32;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct CasingContext {
    target: Arc<str>,
    path: Vec<u8>,
}

#[derive(Clone, Debug)]
struct CasingVariant {
    count: u64,
    first_candidate_id: i64,
}

#[derive(Clone, Debug, Default)]
struct CasingState {
    normalize: bool,
    strategy: String,
    pins: Arc<BTreeMap<String, String>>,
    variants: PersistentHashMap<CasingContext, Arc<BTreeMap<String, CasingVariant>>>,
    canonical: PersistentHashMap<CasingContext, String>,
    postings: PersistentHashMap<CasingContext, PersistentHashSet<PathKey>>,
}

impl CasingState {
    fn from_intent(intent: &ProfileIntent) -> Self {
        Self {
            normalize: intent.normalize_folder_case,
            strategy: match intent.casing_strategy.as_str() {
                "lower" | "force_lower" | "force_upper" => intent.casing_strategy.clone(),
                _ => "upper".to_owned(),
            },
            pins: Arc::new(
                intent
                    .casing_pins
                    .iter()
                    .map(|(key, value)| (key.to_lowercase(), value.clone()))
                    .collect(),
            ),
            ..Self::default()
        }
    }

    fn candidate_contexts(candidate: &Candidate) -> Vec<(CasingContext, String)> {
        let display: Vec<_> = candidate.destination_display.split(['/', '\\']).collect();
        let keys: Vec<_> = candidate
            .destination_key
            .split(|byte| *byte == b'/')
            .collect();
        if display.len() != keys.len() || display.len() < 2 {
            return Vec::new();
        }
        let mut path = Vec::new();
        let mut result = Vec::with_capacity(display.len() - 1);
        for index in 0..display.len() - 1 {
            if !path.is_empty() {
                path.push(b'/');
            }
            path.extend_from_slice(keys[index]);
            result.push((
                CasingContext {
                    target: candidate.target.clone(),
                    path: path.clone(),
                },
                display[index].to_owned(),
            ));
        }
        result
    }

    fn pick(&self, variants: &BTreeMap<String, CasingVariant>) -> Option<String> {
        let prefer_lower = self.strategy == "lower";
        variants
            .iter()
            .min_by(|(left_name, left), (right_name, right)| {
                let left_upper = left_name
                    .chars()
                    .filter(|value| value.is_uppercase())
                    .count();
                let right_upper = right_name
                    .chars()
                    .filter(|value| value.is_uppercase())
                    .count();
                let casing = if prefer_lower {
                    left_upper.cmp(&right_upper)
                } else {
                    right_upper.cmp(&left_upper)
                };
                casing
                    .then_with(|| left.first_candidate_id.cmp(&right.first_candidate_id))
                    .then_with(|| left_name.cmp(right_name))
            })
            .map(|(name, _)| name.clone())
    }

    fn update_context(&mut self, context: &CasingContext) {
        let selected = self
            .variants
            .get(context)
            .and_then(|variants| self.pick(variants));
        if let Some(selected) = selected {
            self.canonical.insert(context.clone(), selected);
        } else {
            self.canonical.remove(context);
        }
    }

    fn add(&mut self, key: &PathKey, candidate: &Candidate) -> HashSet<CasingContext> {
        if key.namespace == Namespace::Archive || !self.normalize {
            return HashSet::new();
        }
        let mut changed = HashSet::new();
        for (context, spelling) in Self::candidate_contexts(candidate) {
            let before = self.canonical.get(&context).cloned();
            let mut variants = self
                .variants
                .get(&context)
                .map(|values| (**values).clone())
                .unwrap_or_default();
            let value = variants.entry(spelling).or_insert(CasingVariant {
                count: 0,
                first_candidate_id: candidate.id,
            });
            value.count = value.count.saturating_add(1);
            value.first_candidate_id = value.first_candidate_id.min(candidate.id);
            self.variants.insert(context.clone(), Arc::new(variants));

            let mut postings = self.postings.get(&context).cloned().unwrap_or_default();
            postings.insert(key.clone());
            self.postings.insert(context.clone(), postings);
            self.update_context(&context);
            if before != self.canonical.get(&context).cloned() {
                changed.insert(context);
            }
        }
        changed
    }

    fn remove(&mut self, key: &PathKey, candidate: &Candidate) -> HashSet<CasingContext> {
        if key.namespace == Namespace::Archive || !self.normalize {
            return HashSet::new();
        }
        let mut changed = HashSet::new();
        for (context, spelling) in Self::candidate_contexts(candidate) {
            let before = self.canonical.get(&context).cloned();
            if let Some(current) = self.variants.get(&context) {
                let mut variants = (**current).clone();
                if let Some(value) = variants.get_mut(&spelling) {
                    value.count = value.count.saturating_sub(1);
                    if value.count == 0 {
                        variants.remove(&spelling);
                    }
                }
                if variants.is_empty() {
                    self.variants.remove(&context);
                } else {
                    self.variants.insert(context.clone(), Arc::new(variants));
                }
            }
            if let Some(current) = self.postings.get(&context) {
                let mut postings = current.clone();
                postings.remove(key);
                if postings.is_empty() {
                    self.postings.remove(&context);
                } else {
                    self.postings.insert(context.clone(), postings);
                }
            }
            self.update_context(&context);
            if before != self.canonical.get(&context).cloned() {
                changed.insert(context);
            }
        }
        changed
    }

    fn apply(&self, candidate: &Candidate) -> String {
        let mut parts: Vec<String> = candidate
            .destination_display
            .split(['/', '\\'])
            .map(str::to_owned)
            .collect();
        let keys: Vec<_> = candidate
            .destination_key
            .split(|byte| *byte == b'/')
            .collect();
        if parts.len() == keys.len() {
            let mut path = Vec::new();
            let folder_count = parts.len().saturating_sub(1);
            for index in 0..folder_count {
                if !path.is_empty() {
                    path.push(b'/');
                }
                path.extend_from_slice(keys[index]);
                if self.normalize {
                    parts[index] = match self.strategy.as_str() {
                        "force_lower" => parts[index].to_lowercase(),
                        "force_upper" => parts[index].to_uppercase(),
                        _ => self
                            .canonical
                            .get(&CasingContext {
                                target: candidate.target.clone(),
                                path: path.clone(),
                            })
                            .cloned()
                            .unwrap_or_else(|| parts[index].clone()),
                    };
                }
            }
        }
        for part in &mut parts {
            if let Some(pinned) = self.pins.get(&part.to_lowercase()) {
                *part = pinned.clone();
            }
        }
        parts.join("/")
    }

    fn legacy(&self, candidate: &Candidate, destination_display: &str) -> String {
        let legacy = candidate.legacy_rel.replace('\\', "/");
        let destination_parts: Vec<_> = destination_display.split('/').collect();
        let legacy_parts: Vec<_> = legacy.split('/').collect();
        let destination_key_parts: Vec<_> = candidate
            .destination_key
            .split(|byte| *byte == b'/')
            .collect();
        let legacy_key = legacy.to_lowercase();
        let legacy_key_parts: Vec<_> = legacy_key.split('/').collect();
        if destination_parts.len() >= legacy_parts.len()
            && destination_key_parts.len() >= legacy_key_parts.len()
            && destination_key_parts[destination_key_parts.len() - legacy_key_parts.len()..]
                .iter()
                .map(|part| String::from_utf8_lossy(part))
                .eq(legacy_key_parts.iter().copied())
        {
            return destination_parts[destination_parts.len() - legacy_parts.len()..].join("/");
        }
        legacy_parts
            .into_iter()
            .enumerate()
            .map(|(index, part)| {
                let mut value = if self.normalize && index + 1 < legacy_key_parts.len() {
                    match self.strategy.as_str() {
                        "force_lower" => part.to_lowercase(),
                        "force_upper" => part.to_uppercase(),
                        _ => part.to_owned(),
                    }
                } else {
                    part.to_owned()
                };
                if let Some(pinned) = self.pins.get(&value.to_lowercase()) {
                    value = pinned.clone();
                }
                value
            })
            .collect::<Vec<_>>()
            .join("/")
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum CounterKind {
    Loose,
    Archive,
    Identity,
}

#[derive(Clone, Debug)]
struct CounterDelta {
    mod_id: i64,
    kind: CounterKind,
    wins: u32,
    losses: u32,
    surviving: u32,
    files: u32,
    flags: u32,
}

impl CounterDelta {
    fn files(mod_id: i64, kind: CounterKind, flags: u32) -> Self {
        Self {
            mod_id,
            kind,
            wins: 0,
            losses: 0,
            surviving: 0,
            files: 1,
            flags,
        }
    }

    fn surviving(mod_id: i64, kind: CounterKind) -> Self {
        Self {
            mod_id,
            kind,
            wins: 0,
            losses: 0,
            surviving: 1,
            files: 0,
            flags: 0,
        }
    }

    fn edge_loser(mod_id: i64, kind: CounterKind) -> Self {
        Self {
            mod_id,
            kind,
            wins: 0,
            losses: 1,
            surviving: 0,
            files: 0,
            flags: 0,
        }
    }

    fn edge_winner(mod_id: i64, kind: CounterKind) -> Self {
        Self {
            mod_id,
            kind,
            wins: 1,
            losses: 0,
            surviving: 0,
            files: 0,
            flags: 0,
        }
    }

    fn remove_surviving(mod_id: i64, kind: CounterKind) -> Self {
        Self {
            mod_id,
            kind,
            wins: 0,
            losses: 0,
            surviving: u32::MAX,
            files: 0,
            flags: 0,
        }
    }
}

#[derive(Clone, Debug, Default)]
struct KindCounter {
    wins: u64,
    losses: u64,
    surviving: u64,
    files: u64,
}

#[derive(Clone, Debug, Default)]
struct ModCounters {
    loose: KindCounter,
    archive: KindCounter,
    identity: KindCounter,
    flag_counts: [u32; 32],
}

#[derive(Clone, Debug, Default)]
struct CounterAccum {
    wins: i64,
    losses: i64,
    surviving: i64,
    files: i64,
    flags: [i64; 32],
}

impl ModCounters {
    fn flags(&self) -> u32 {
        self.flag_counts
            .iter()
            .enumerate()
            .fold(0_u32, |value, (bit, count)| {
                if *count > 0 {
                    value | (1_u32 << bit)
                } else {
                    value
                }
            })
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) enum ConflictKind {
    Loose,
    Archive,
    Identity,
    LooseArchive,
}

impl ConflictKind {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Loose => "loose",
            Self::Archive => "archive",
            Self::Identity => "identity",
            Self::LooseArchive => "loose_archive",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct EdgeKey {
    pub(crate) kind: ConflictKind,
    pub(crate) loser: i64,
    pub(crate) winner: i64,
}

#[derive(Clone, Debug, Default)]
struct DestinationState {
    providers: SmallVec<[ProviderIndex; 4]>,
    normal_end: u32,
    root_end: u32,
    loose_archive_conflict: bool,
}

impl DestinationState {
    fn stack(&self, namespace: Namespace) -> &[ProviderIndex] {
        match namespace {
            Namespace::Normal => &self.providers[..self.normal_end as usize],
            Namespace::Root => &self.providers[self.normal_end as usize..self.root_end as usize],
            Namespace::Archive => &self.providers[self.root_end as usize..],
        }
    }

    fn published(&self, namespace: Namespace) -> Option<ProviderIndex> {
        self.stack(namespace).last().copied()
    }
}

#[derive(Clone, Debug, Default)]
struct IdentityState {
    providers: SmallVec<[ProviderIndex; 4]>,
}

impl IdentityState {
    fn suppressed(&self) -> &[ProviderIndex] {
        &self.providers[..self.providers.len().saturating_sub(1)]
    }
}

#[derive(Clone, Debug)]
struct GraphInventory {
    candidate_indexes: HashMap<i64, ProviderIndex>,
    /// Candidate IDs present in the catalog projection selected by the
    /// current intent. Older candidates may remain in stable provider slots
    /// across a targeted inventory replacement so existing destination
    /// stacks do not need to be rebuilt or renumbered.
    active_candidate_ids: HashSet<i64>,
    effective_paths: Vec<EffectivePath>,
    effective_lookup: HashMap<EffectivePath, EffectiveKey>,
    effective_postings: Vec<Vec<ProviderIndex>>,
    candidate_effective: Vec<EffectiveKey>,
    identity_postings: HashMap<Arc<[u8]>, Vec<ProviderIndex>>,
    mod_candidates: HashMap<Arc<str>, Vec<ProviderIndex>>,
    archive_candidates: HashMap<String, Vec<ProviderIndex>>,
    plugin_paths: HashMap<String, Vec<PathKey>>,
    mod_names: HashMap<i64, Arc<str>>,
    mod_keys: HashMap<i64, Arc<str>>,
    mod_ids_by_key: HashMap<Arc<str>, i64>,
}

impl GraphInventory {
    fn new(candidates: &[Candidate]) -> Self {
        let active_candidate_ids = candidates.iter().map(|candidate| candidate.id).collect();
        Self::new_with_active(candidates, active_candidate_ids)
    }

    fn new_with_active(candidates: &[Candidate], active_candidate_ids: HashSet<i64>) -> Self {
        let mut candidate_indexes = HashMap::with_capacity(candidates.len());
        let mut effective_paths = Vec::new();
        let mut effective_lookup = HashMap::new();
        let mut effective_postings: Vec<Vec<ProviderIndex>> = Vec::new();
        let mut candidate_effective = Vec::with_capacity(candidates.len());
        let mut identity_postings: HashMap<Arc<[u8]>, Vec<ProviderIndex>> = HashMap::new();
        let mut mod_candidates: HashMap<Arc<str>, Vec<ProviderIndex>> = HashMap::new();
        let mut archive_candidates: HashMap<String, Vec<ProviderIndex>> = HashMap::new();
        let mut plugin_path_sets: HashMap<String, HashSet<PathKey>> = HashMap::new();
        let mut mod_names = HashMap::new();
        let mut mod_keys = HashMap::new();
        let mut mod_ids_by_key = HashMap::new();
        for (index, candidate) in candidates.iter().enumerate() {
            let provider_index = index as ProviderIndex;
            mod_names
                .entry(candidate.mod_id)
                .or_insert_with(|| candidate.mod_name.clone());
            mod_keys
                .entry(candidate.mod_id)
                .or_insert_with(|| candidate.mod_key.clone());
            mod_ids_by_key
                .entry(candidate.mod_key.clone())
                .or_insert(candidate.mod_id);
            candidate_indexes.insert(candidate.id, provider_index);
            mod_candidates
                .entry(candidate.mod_key.clone())
                .or_default()
                .push(provider_index);
            let path = EffectivePath {
                target: candidate.target.clone(),
                path: candidate.destination_key.clone(),
            };
            let effective = if let Some(effective) = effective_lookup.get(&path) {
                *effective
            } else {
                let effective = effective_paths.len() as u32;
                effective_paths.push(path.clone());
                effective_lookup.insert(path, effective);
                effective_postings.push(Vec::new());
                effective
            };
            candidate_effective.push(effective);
            effective_postings[effective as usize].push(provider_index);
            if let Some(archive) = &candidate.archive_key {
                archive_candidates
                    .entry(archive.to_lowercase())
                    .or_default()
                    .push(provider_index);
            }
            if candidate.kind != ProviderKind::ArchiveMember
                && let Some(plugin) = &candidate.plugin_key
            {
                plugin_path_sets
                    .entry(plugin.to_lowercase())
                    .or_default()
                    .insert(PathKey {
                        namespace: candidate.kind.namespace(),
                        effective,
                    });
            }
            for identity in &candidate.identities {
                identity_postings
                    .entry(identity.clone())
                    .or_default()
                    .push(provider_index);
            }
        }
        Self {
            candidate_indexes,
            active_candidate_ids,
            effective_paths,
            effective_lookup,
            effective_postings,
            candidate_effective,
            identity_postings,
            mod_candidates,
            archive_candidates,
            plugin_paths: plugin_path_sets
                .into_iter()
                .map(|(key, values)| (key, values.into_iter().collect()))
                .collect(),
            mod_names,
            mod_keys,
            mod_ids_by_key,
        }
    }

    fn effective(&self, target: &str, path: &[u8]) -> Option<EffectiveKey> {
        self.effective_lookup
            .get(&EffectivePath {
                target: Arc::from(target),
                path: Arc::from(path),
            })
            .copied()
    }

    fn path(&self, effective: EffectiveKey) -> Option<&EffectivePath> {
        self.effective_paths.get(effective as usize)
    }

    fn mod_name(&self, mod_id: i64) -> Option<&str> {
        self.mod_names.get(&mod_id).map(AsRef::as_ref)
    }

    fn mod_id(&self, name: &str) -> Option<i64> {
        let name = name.to_lowercase();
        self.mod_ids_by_key.get(name.as_str()).copied()
    }
}

struct RankContext {
    mod_rank: HashMap<i64, i64>,
    archive_rank: HashMap<String, i64>,
    plugin_rank: HashMap<String, i64>,
    enabled: HashSet<i64>,
}

impl RankContext {
    fn new(intent: &ProfileIntent, inventory: &GraphInventory) -> Self {
        let count = intent.mods.len() as i64;
        let mut mod_rank = HashMap::with_capacity(intent.mods.len());
        let mut enabled = HashSet::with_capacity(intent.mods.len() + 1);
        for (index, entry) in intent.mods.iter().enumerate() {
            if let Some(mod_id) = inventory.mod_ids_by_key.get(entry.key.as_str()) {
                mod_rank.insert(*mod_id, count - index as i64);
                if entry.enabled {
                    enabled.insert(*mod_id);
                }
            }
        }
        for special in ["[overwrite]", "[root_folder]"] {
            if let Some(mod_id) = inventory.mod_ids_by_key.get(special) {
                enabled.insert(*mod_id);
            }
        }
        let archive_rank = intent
            .archive_order
            .iter()
            .enumerate()
            .map(|(index, key)| (key.to_lowercase(), index as i64 + 1))
            .collect();
        let plugin_rank = intent
            .plugin_order
            .iter()
            .enumerate()
            .map(|(index, key)| (key.to_lowercase(), index as i64 + 1))
            .collect();
        Self {
            mod_rank,
            archive_rank,
            plugin_rank,
            enabled,
        }
    }

    fn active(&self, candidate: &Candidate) -> bool {
        self.enabled.contains(&candidate.mod_id)
    }

    fn rank(&self, candidate: &Candidate) -> (i64, i64, u32, i64) {
        let normal_rank = if candidate.kind == ProviderKind::Overwrite {
            i64::MAX / 4
        } else {
            *self.mod_rank.get(&candidate.mod_id).unwrap_or(&0)
        };
        if candidate.kind == ProviderKind::ArchiveMember {
            let archive_rank = candidate
                .archive_key
                .as_ref()
                .and_then(|key| self.archive_rank.get(&key.to_lowercase()))
                .copied()
                .unwrap_or(normal_rank);
            if let Some(plugin_key) = &candidate.plugin_key {
                // Plugin-owned archives load after unowned archives. Their
                // current plugin position is profile intent, not inventory,
                // so changing loadorder.txt never requires an archive rescan.
                let plugin_rank = self
                    .plugin_rank
                    .get(&plugin_key.to_lowercase())
                    .copied()
                    .unwrap_or(normal_rank);
                (
                    i64::MAX / 8 + plugin_rank,
                    archive_rank,
                    candidate.ordinal,
                    candidate.id,
                )
            } else {
                (archive_rank, normal_rank, candidate.ordinal, candidate.id)
            }
        } else {
            (normal_rank, normal_rank, candidate.ordinal, candidate.id)
        }
    }
}

#[derive(Clone, Debug)]
pub struct GraphSnapshot {
    pub generation: u64,
    pub inventory_generation: u64,
    pub candidates: Arc<Vec<Candidate>>,
    raw_files: Arc<Vec<RawCatalogFile>>,
    inventory: Arc<GraphInventory>,
    destination_states: PersistentHashMap<EffectiveKey, Arc<DestinationState>>,
    identity_states: PersistentHashMap<Arc<[u8]>, Arc<IdentityState>>,
    suppressed_counts: PersistentHashMap<i64, u32>,
    pub(crate) winners: PersistentHashMap<PathKey, i64>,
    counters: HashMap<i64, ModCounters>,
    pub(crate) summaries: BTreeMap<String, ConflictSummary>,
    pub(crate) edges: PersistentHashMap<EdgeKey, u64>,
    plugin_owners: BTreeMap<String, String>,
    capability_flags: BTreeMap<String, u32>,
    casing: CasingState,
    enabled_mods: Arc<HashSet<String>>,
    selected_variants: Arc<HashMap<String, String>>,
    deployed_paths: PersistentHashMap<Vec<u8>, u32>,
    deployed_basenames: PersistentHashMap<Vec<u8>, u32>,
    loose_beats_archive: bool,
    rules_hash: Vec<u8>,
}

pub struct GraphUpdate {
    pub snapshot: GraphSnapshot,
    pub delta: ResolutionDelta,
    pub(crate) changed_winner_keys: HashSet<PathKey>,
    pub(crate) changed_edge_keys: HashSet<EdgeKey>,
    pub(crate) changed_summary_names: BTreeSet<String>,
}

impl GraphSnapshot {
    pub(crate) fn raw_file_count(&self) -> usize {
        self.raw_files.len()
    }

    pub fn loose_beats_archive(&self) -> bool {
        self.loose_beats_archive
    }

    pub fn empty(inventory_generation: u64) -> Self {
        Self {
            generation: 0,
            inventory_generation,
            candidates: Arc::new(Vec::new()),
            raw_files: Arc::new(Vec::new()),
            inventory: Arc::new(GraphInventory::new(&[])),
            destination_states: PersistentHashMap::new(),
            identity_states: PersistentHashMap::new(),
            suppressed_counts: PersistentHashMap::new(),
            winners: PersistentHashMap::new(),
            counters: HashMap::new(),
            summaries: BTreeMap::new(),
            edges: PersistentHashMap::new(),
            plugin_owners: BTreeMap::new(),
            capability_flags: BTreeMap::new(),
            casing: CasingState::default(),
            enabled_mods: Arc::new(HashSet::new()),
            selected_variants: Arc::new(HashMap::new()),
            deployed_paths: PersistentHashMap::new(),
            deployed_basenames: PersistentHashMap::new(),
            loose_beats_archive: false,
            rules_hash: Vec::new(),
        }
    }

    pub(crate) fn candidate(&self, id: i64) -> Option<&Candidate> {
        self.inventory
            .candidate_indexes
            .get(&id)
            .and_then(|index| self.candidates.get(*index as usize))
    }

    fn candidate_at(&self, index: ProviderIndex) -> Option<&Candidate> {
        self.candidates.get(index as usize)
    }

    pub fn export(&self) -> SnapshotExport {
        let mut winners: Vec<_> = self
            .winners
            .iter()
            .filter_map(|(key, id)| {
                self.candidate(*id)
                    .map(|candidate| self.winner_record(candidate, key.namespace))
            })
            .collect();
        winners.sort_by(|left, right| {
            (left.namespace, &left.target, &left.destination_key).cmp(&(
                right.namespace,
                &right.target,
                &right.destination_key,
            ))
        });
        let mut edges: Vec<_> = self
            .edges
            .iter()
            .filter_map(|(edge, count)| self.edge_record(edge, *count))
            .collect();
        edges.sort_by(|left, right| {
            (&left.kind, &left.loser, &left.winner).cmp(&(&right.kind, &right.loser, &right.winner))
        });
        SnapshotExport {
            generation: self.generation,
            inventory_generation: self.inventory_generation,
            winners,
            summaries: self.summaries.clone(),
            edges,
            plugin_owners: self.plugin_owners.clone(),
            capability_flags: self.capability_flags.clone(),
        }
    }

    pub fn iter_winners(
        &self,
        target: Option<&str>,
        namespaces: &BTreeSet<Namespace>,
        after_id: i64,
        limit: usize,
    ) -> Vec<WinnerRecord> {
        let mut rows: Vec<_> = self
            .winners
            .iter()
            .filter_map(|(key, candidate_id)| {
                let candidate = self.candidate(*candidate_id)?;
                if *candidate_id <= after_id
                    || target.is_some_and(|value| candidate.target.as_ref() != value)
                    || (!namespaces.is_empty() && !namespaces.contains(&key.namespace))
                {
                    return None;
                }
                Some(self.winner_record(candidate, key.namespace))
            })
            .collect();
        rows.sort_by_key(|winner| winner.candidate_id);
        rows.truncate(limit);
        rows
    }

    pub fn conflict_state(&self) -> ConflictStateExport {
        let mut edges: Vec<_> = self
            .edges
            .iter()
            .filter_map(|(key, refcount)| self.edge_record(key, *refcount))
            .collect();
        edges.sort_by(|left, right| {
            (&left.kind, &left.loser, &left.winner).cmp(&(&right.kind, &right.loser, &right.winner))
        });
        ConflictStateExport {
            generation: self.generation,
            summaries: self.summaries.clone(),
            edges,
            plugin_owners: self.plugin_owners.clone(),
            archive_plugin_stems: self.archive_plugin_stems(),
            capability_flags: self.capability_flags.clone(),
        }
    }

    fn archive_plugin_stems(&self) -> BTreeMap<String, BTreeSet<String>> {
        let mut result: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for candidate in self.candidates.iter().filter(|candidate| {
            candidate.kind == ProviderKind::ArchiveMember
                && self.enabled(candidate)
                && self.selected(candidate)
        }) {
            if let Some(plugin) = &candidate.plugin_key {
                result
                    .entry(candidate.mod_name.to_string())
                    .or_default()
                    .insert(plugin.to_lowercase());
            }
        }
        result
    }

    pub fn framework_winners(&self) -> Vec<WinnerRecord> {
        self.flagged_winners(1 << 4)
    }

    pub fn flagged_winners(&self, flags: u32) -> Vec<WinnerRecord> {
        self.winners
            .iter()
            .filter_map(|(key, candidate_id)| {
                let candidate = self.candidate(*candidate_id)?;
                (candidate.flags & flags != 0).then(|| self.winner_record(candidate, key.namespace))
            })
            .collect()
    }

    pub fn staged_plugins(&self) -> BTreeSet<String> {
        self.raw_files
            .iter()
            .filter(|raw| raw.flags & (1 << 7) != 0 && raw.mod_key.as_ref() != "[overwrite]")
            .map(|raw| {
                raw.source_display
                    .replace('\\', "/")
                    .rsplit('/')
                    .next()
                    .unwrap_or(&raw.source_display)
                    .to_lowercase()
            })
            .collect()
    }

    pub fn plugin_winners(&self) -> BTreeMap<String, WinnerRecord> {
        self.inventory
            .plugin_paths
            .keys()
            .filter_map(|plugin| {
                let (path, candidate_id) = plugin_candidate(self, plugin)?;
                let candidate = self.candidate(candidate_id)?;
                Some((
                    plugin.clone(),
                    self.winner_record(candidate, path.namespace),
                ))
            })
            .collect()
    }

    pub fn has_deployed_path(&self, path: &[u8], basename: bool) -> bool {
        if basename {
            self.deployed_basenames.contains_key(path)
        } else {
            self.deployed_paths.contains_key(path)
        }
    }

    pub fn patch_files(&self) -> Vec<(String, Vec<u8>)> {
        self.raw_files
            .iter()
            .filter_map(|raw| {
                if raw.flags & (1 << 6) == 0 {
                    return None;
                }
                let lower = raw.index_display.replace('\\', "/").to_lowercase();
                let basename = lower.rsplit('/').next().unwrap_or(&lower);
                let relevant = basename.ends_with(".esp")
                    || basename.ends_with(".esm")
                    || basename.ends_with(".esl")
                    || basename.ends_with("_swap.ini")
                    || (basename.ends_with(".ini")
                        && (lower.contains("/skse/plugins/skypatcher/")
                            || lower.contains("/skse/plugins/skypatcher2/")));
                relevant.then(|| (raw.mod_name.to_string(), raw.source_rel.to_vec()))
            })
            .collect()
    }

    fn selected(&self, candidate: &Candidate) -> bool {
        self.inventory.active_candidate_ids.contains(&candidate.id)
            && (candidate.kind == ProviderKind::Overwrite
                || (candidate.kind == ProviderKind::Root
                    && candidate.mod_key.as_ref() == "[root_folder]")
                || self
                    .selected_variants
                    .get(candidate.mod_key.as_ref())
                    .is_some_and(|variant| {
                        variant.is_empty() || variant.as_str() == candidate.variant_key.as_ref()
                    }))
    }

    fn enabled(&self, candidate: &Candidate) -> bool {
        candidate.kind == ProviderKind::Overwrite
            || (candidate.kind == ProviderKind::Root
                && candidate.mod_key.as_ref() == "[root_folder]")
            || self.enabled_mods.contains(candidate.mod_key.as_ref())
    }

    /// Return only the plugin spellings needed by activation sync.
    ///
    /// The former caller requested `mod_files()` and then discarded every
    /// non-plugin record. For patcher outputs that meant constructing,
    /// serialising, decoding, and allocating tens of thousands of rich
    /// conflict records to discover one or two plugin names.
    pub fn mod_plugins(&self, mod_name: &str) -> Vec<String> {
        let mod_key = mod_name.to_lowercase();
        let mut selected_plugins: HashMap<&[u8], ProviderIndex> = HashMap::new();
        for index in self
            .inventory
            .mod_candidates
            .get(mod_key.as_str())
            .into_iter()
            .flatten()
        {
            let candidate = &self.candidates[*index as usize];
            if candidate.kind == ProviderKind::ArchiveMember
                || candidate.plugin_key.is_none()
                || !self.selected(candidate)
            {
                continue;
            }
            selected_plugins
                .entry(candidate.source_rel.as_ref())
                .or_insert(*index);
        }

        let mut plugins: Vec<(&[u8], String)> = self
            .raw_files
            .iter()
            .filter(|raw| raw.mod_key.as_ref() == mod_key && raw.flags & (1 << 7) != 0)
            .map(|raw| {
                let spelling = selected_plugins
                    .get(raw.source_rel.as_ref())
                    .and_then(|index| self.candidates.get(*index as usize))
                    .map(|candidate| self.casing.apply(candidate))
                    .unwrap_or_else(|| raw.index_display.to_string());
                let basename = spelling
                    .replace('\\', "/")
                    .rsplit('/')
                    .next()
                    .unwrap_or(&spelling)
                    .to_owned();
                (raw.source_rel.as_ref(), basename)
            })
            .collect();
        plugins.sort_by(|left, right| (left.0, &left.1).cmp(&(right.0, &right.1)));
        plugins.into_iter().map(|(_source, name)| name).collect()
    }

    pub fn mod_files(&self, mod_name: &str) -> Vec<ModFileRecord> {
        let mod_key = mod_name.to_lowercase();
        let mut selected_candidates: HashMap<&[u8], ProviderIndex> = HashMap::new();
        for index in self
            .inventory
            .mod_candidates
            .get(mod_key.as_str())
            .into_iter()
            .flatten()
        {
            let candidate = &self.candidates[*index as usize];
            if candidate.kind == ProviderKind::ArchiveMember || !self.selected(candidate) {
                continue;
            }
            selected_candidates
                .entry(candidate.source_rel.as_ref())
                .and_modify(|existing| {
                    let previous = &self.candidates[*existing as usize];
                    if previous.plugin_key.is_none() && candidate.plugin_key.is_some() {
                        *existing = *index;
                    }
                })
                .or_insert(*index);
        }
        let mut result = Vec::new();
        for raw in self
            .raw_files
            .iter()
            .filter(|raw| raw.mod_key.as_ref() == mod_key)
        {
            let candidate = selected_candidates
                .get(raw.source_rel.as_ref())
                .and_then(|index| self.candidates.get(*index as usize));
            let Some(candidate) = candidate else {
                let plugin_key = (raw.flags & (1 << 7) != 0).then(|| {
                    raw.source_display
                        .replace('\\', "/")
                        .rsplit('/')
                        .next()
                        .unwrap_or(&raw.source_display)
                        .to_lowercase()
                });
                result.push(ModFileRecord {
                    candidate_id: 0,
                    mod_name: raw.mod_name.to_string(),
                    source_rel: raw.source_rel.to_vec(),
                    source_display: raw.source_display.to_string(),
                    target: String::new(),
                    destination_key: raw.index_display.as_bytes().to_vec(),
                    destination_display: raw.index_display.to_string(),
                    namespace: if raw.flags & (1 << 8) != 0 {
                        Namespace::Root
                    } else {
                        Namespace::Normal
                    },
                    provider_kind: if raw.flags & (1 << 8) != 0 {
                        ProviderKind::Root
                    } else {
                        ProviderKind::Loose
                    },
                    enabled: self.enabled_mods.contains(raw.mod_key.as_ref()),
                    winning: false,
                    conflict_status: 0,
                    deployable: false,
                    flags: raw.flags,
                    plugin_key,
                    legacy_rel: raw.index_display.to_string(),
                });
                continue;
            };
            let namespace = candidate.kind.namespace();
            let effective = self
                .inventory
                .effective(&candidate.target, &candidate.destination_key)
                .expect("candidate destination is interned");
            let key = PathKey {
                namespace,
                effective,
            };
            let winning = self
                .winners
                .get(&key)
                .and_then(|id| self.candidate(*id))
                .is_some_and(|winner| winner.mod_key == candidate.mod_key);
            let identity_loser = self
                .suppressed_counts
                .get(&candidate.id)
                .copied()
                .unwrap_or(0)
                > 0;
            let contested = self
                .destination_states
                .get(&effective)
                .is_some_and(|state| {
                    destination_edges(state, self).iter().any(|edge| {
                        edge.loser == candidate.mod_id || edge.winner == candidate.mod_id
                    })
                })
                || candidate.identities.iter().any(|identity| {
                    self.identity_states
                        .get(identity.as_ref())
                        .is_some_and(|state| {
                            identity_edges(state, self).iter().any(|edge| {
                                edge.loser == candidate.mod_id || edge.winner == candidate.mod_id
                            }) || state.suppressed().iter().any(|index| {
                                self.candidate_at(*index)
                                    .is_some_and(|provider| provider.id == candidate.id)
                            })
                        })
                });
            let conflict_status = if !self.enabled(candidate) || !contested {
                0
            } else if identity_loser {
                -1
            } else if winning {
                1
            } else {
                -1
            };
            result.push(ModFileRecord {
                candidate_id: candidate.id,
                mod_name: candidate.mod_name.to_string(),
                source_rel: raw.source_rel.to_vec(),
                source_display: raw.source_display.to_string(),
                target: candidate.target.to_string(),
                destination_key: candidate.destination_key.to_vec(),
                destination_display: self.casing.apply(candidate),
                namespace,
                provider_kind: candidate.kind,
                enabled: self.enabled(candidate),
                winning,
                conflict_status,
                deployable: candidate.deployable,
                flags: candidate.flags,
                plugin_key: candidate.plugin_key.as_deref().map(str::to_owned),
                legacy_rel: candidate.legacy_rel.to_string(),
            });
        }
        result.sort_by(|left, right| {
            (&left.source_rel, left.candidate_id).cmp(&(&right.source_rel, right.candidate_id))
        });
        result
    }

    pub fn archive_files(&self, mod_name: &str) -> Vec<ModFileRecord> {
        let mod_key = mod_name.to_lowercase();
        let mut result = Vec::new();
        for candidate in self.candidates.iter().filter(|candidate| {
            candidate.kind == ProviderKind::ArchiveMember
                && candidate.mod_key.as_ref() == mod_key
                && self.selected(candidate)
        }) {
            let effective = self
                .inventory
                .effective(&candidate.target, &candidate.destination_key)
                .expect("candidate destination is interned");
            let key = PathKey {
                namespace: Namespace::Archive,
                effective,
            };
            let winning = self.winners.get(&key).is_some_and(|winner| {
                self.candidate(*winner)
                    .is_some_and(|winner| winner.mod_key == candidate.mod_key)
            });
            let conflict_status = if !self.enabled(candidate) {
                0
            } else {
                self.destination_states.get(&effective).map_or(0, |state| {
                    let edges = destination_edges(state, self);
                    if edges.iter().any(|edge| edge.loser == candidate.mod_id) {
                        -1
                    } else if edges.iter().any(|edge| edge.winner == candidate.mod_id) {
                        1
                    } else {
                        0
                    }
                })
            };
            result.push(ModFileRecord {
                candidate_id: candidate.id,
                mod_name: candidate.mod_name.to_string(),
                source_rel: candidate.source_rel.to_vec(),
                source_display: candidate.source_display.to_string(),
                target: candidate.target.to_string(),
                destination_key: candidate.destination_key.to_vec(),
                destination_display: candidate.legacy_rel.to_string(),
                namespace: Namespace::Archive,
                provider_kind: ProviderKind::ArchiveMember,
                enabled: self.enabled(candidate),
                winning,
                conflict_status,
                deployable: false,
                flags: candidate.flags,
                plugin_key: candidate.plugin_key.as_deref().map(str::to_owned),
                legacy_rel: candidate.legacy_rel.to_string(),
            });
        }
        result.sort_by(|left, right| {
            (&left.source_rel, &left.destination_key, left.candidate_id).cmp(&(
                &right.source_rel,
                &right.destination_key,
                right.candidate_id,
            ))
        });
        result
    }

    pub fn iter_mod_files(
        &self,
        mod_name: &str,
        winners_only: bool,
        kinds: &BTreeSet<ProviderKind>,
        cursor: usize,
        limit: usize,
    ) -> Vec<ModFileRecord> {
        let mut rows = self.mod_files(mod_name);
        rows.retain(|record| {
            (!winners_only || record.winning)
                && (kinds.is_empty() || kinds.contains(&record.provider_kind))
        });
        rows.into_iter().skip(cursor).take(limit).collect()
    }

    pub fn inventory_facets(&self) -> InventoryFacets {
        const PBR_SUFFIXES: [&str; 9] = [
            "_p", "_m", "_em", "_envmask", "_rmaos", "_cnr", "_s", "_i", "_f",
        ];
        let mut result = InventoryFacets::default();
        for raw in self
            .raw_files
            .iter()
            .filter(|raw| raw.flags & (1 << 6) != 0)
        {
            let path = raw.source_display.replace('\\', "/").to_lowercase();
            let basename = path.rsplit('/').next().unwrap_or(&path);
            let extension = basename
                .rfind('.')
                .map(|index| basename[index..].to_owned())
                .filter(|value| value.len() > 1);
            if let Some(extension) = extension {
                *result.filetype_counts.entry(extension.clone()).or_insert(0) += 1;
                result
                    .mod_filetypes
                    .entry(raw.mod_name.to_string())
                    .or_default()
                    .insert(extension);
            }
            if let Some(stem) = basename.strip_suffix(".dds")
                && PBR_SUFFIXES.iter().any(|suffix| stem.ends_with(suffix))
            {
                result.mods_with_pbr.insert(raw.mod_name.to_string());
            }
        }
        for (mod_name, flags) in &self.capability_flags {
            if flags & (1 << 2) != 0 {
                result.mods_with_plugins.insert(mod_name.clone());
            }
            if flags & (1 << 3) != 0 {
                result.mods_with_archives.insert(mod_name.clone());
            }
        }
        result
    }

    pub fn raw_files_by_basename(&self, basenames: &BTreeSet<String>) -> Vec<(String, Vec<u8>)> {
        let mut result: Vec<_> = self
            .raw_files
            .iter()
            .filter_map(|raw| {
                if raw.flags & (1 << 6) == 0 {
                    return None;
                }
                let path = raw.source_display.replace('\\', "/").to_lowercase();
                let basename = path.rsplit('/').next().unwrap_or(&path);
                basenames
                    .contains(basename)
                    .then(|| (raw.mod_name.to_string(), raw.source_rel.to_vec()))
            })
            .collect();
        result.sort();
        result
    }

    pub fn winner_by_suffix(&self, suffix: &[u8]) -> Option<WinnerRecord> {
        let suffix = suffix.strip_prefix(b"/").unwrap_or(suffix);
        self.winners
            .iter()
            .filter_map(|(key, candidate_id)| {
                let path = &self.inventory.path(key.effective)?.path;
                let matches = path.as_ref() == suffix
                    || (path.ends_with(suffix)
                        && path.len() > suffix.len()
                        && path[path.len() - suffix.len() - 1] == b'/');
                matches.then_some((key, candidate_id))
            })
            // Root deployment wins a cross-namespace collision, matching the
            // deployment plan's phase suppression.
            .max_by_key(|(key, _)| match key.namespace {
                Namespace::Root => 2,
                Namespace::Normal => 1,
                Namespace::Archive => 0,
            })
            .and_then(|(key, candidate_id)| {
                self.candidate(*candidate_id)
                    .map(|candidate| self.winner_record(candidate, key.namespace))
            })
    }

    pub fn asset_winners(&self, prefixes: &[String]) -> Vec<WinnerRecord> {
        let prefixes: Vec<_> = prefixes
            .iter()
            .map(|prefix| prefix.replace('\\', "/").to_lowercase())
            .collect();
        let mut result: Vec<_> = self
            .winners
            .iter()
            .filter_map(|(key, candidate_id)| {
                let candidate = self.candidate(*candidate_id)?;
                let relative = candidate.legacy_rel.replace('\\', "/").to_lowercase();
                (prefixes.is_empty() || prefixes.iter().any(|prefix| relative.starts_with(prefix)))
                    .then(|| self.winner_record(candidate, key.namespace))
            })
            .collect();
        result.sort_by_key(|winner| match winner.namespace {
            Namespace::Archive => 0,
            Namespace::Normal => 1,
            Namespace::Root => 2,
        });
        result
    }

    pub fn framework_basenames(&self, mod_names: &BTreeSet<String>) -> BTreeSet<String> {
        let lowered: HashSet<_> = mod_names.iter().map(|name| name.to_lowercase()).collect();
        self.raw_files
            .iter()
            .filter(|raw| raw.flags & (1 << 4) != 0 && lowered.contains(raw.mod_key.as_ref()))
            .map(|raw| {
                raw.source_display
                    .replace('\\', "/")
                    .rsplit('/')
                    .next()
                    .unwrap_or(&raw.source_display)
                    .to_lowercase()
            })
            .collect()
    }

    pub fn contested_paths(&self) -> Vec<(String, Vec<u8>)> {
        let mut result = Vec::new();
        for (effective, state) in &self.destination_states {
            let owners: HashSet<_> = state
                .providers
                .iter()
                .filter_map(|index| self.candidate_at(*index))
                .map(|candidate| candidate.mod_key.as_ref())
                .collect();
            if owners.len() > 1 {
                if let Some(path) = self.inventory.path(*effective) {
                    result.push((path.target.to_string(), path.path.to_vec()));
                }
            }
        }
        result.sort();
        result
    }

    pub fn winner(&self, namespace: Namespace, target: &str, path: &[u8]) -> Option<WinnerRecord> {
        let effective = self.inventory.effective(target, path)?;
        let key = PathKey {
            namespace,
            effective,
        };
        self.candidate(*self.winners.get(&key)?)
            .map(|candidate| self.winner_record(candidate, namespace))
    }

    pub fn providers(
        &self,
        namespace: Namespace,
        target: &str,
        path: &[u8],
    ) -> Vec<ProviderRecord> {
        let Some(effective) = self.inventory.effective(target, path) else {
            return Vec::new();
        };
        let Some(state) = self.destination_states.get(&effective) else {
            return Vec::new();
        };
        let winner = state.published(namespace);
        state
            .stack(namespace)
            .iter()
            .filter_map(|id| {
                self.candidate_at(*id).map(|candidate| ProviderRecord {
                    candidate_id: candidate.id,
                    mod_name: candidate.mod_name.to_string(),
                    kind: candidate.kind,
                    winning: Some(*id) == winner,
                })
            })
            .collect()
    }

    pub fn conflict_summary(&self, mod_name: &str) -> ConflictSummary {
        self.summaries.get(mod_name).cloned().unwrap_or_default()
    }

    pub fn conflict_partners(&self, mod_name: &str, kinds: &BTreeSet<String>) -> BTreeSet<String> {
        let mut result = BTreeSet::new();
        let Some(mod_id) = self.inventory.mod_id(mod_name) else {
            return result;
        };
        for edge in self.edges.keys() {
            if !kinds.is_empty() && !kinds.contains(edge.kind.as_str()) {
                continue;
            }
            if edge.loser == mod_id {
                if let Some(name) = self.inventory.mod_name(edge.winner) {
                    result.insert(name.to_owned());
                }
            } else if edge.winner == mod_id
                && let Some(name) = self.inventory.mod_name(edge.loser)
            {
                result.insert(name.to_owned());
            }
        }
        result
    }

    pub fn conflict_files(
        &self,
        first: &str,
        second: &str,
        kinds: &BTreeSet<String>,
    ) -> Vec<(String, Vec<u8>)> {
        let Some(first_id) = self.inventory.mod_id(first) else {
            return Vec::new();
        };
        let Some(second_id) = self.inventory.mod_id(second) else {
            return Vec::new();
        };
        let matches = |edge: &EdgeKey| {
            (kinds.is_empty() || kinds.contains(edge.kind.as_str()))
                && ((edge.loser == first_id && edge.winner == second_id)
                    || (edge.loser == second_id && edge.winner == first_id))
        };
        let mut result = Vec::new();
        for (effective, state) in &self.destination_states {
            if destination_edges(state, self).iter().any(&matches) {
                if let Some(path) = self.inventory.path(*effective) {
                    result.push((path.target.to_string(), path.path.to_vec()));
                }
            }
        }
        for (identity, state) in &self.identity_states {
            if identity_edges(state, self).iter().any(&matches) {
                result.push(("identity".to_owned(), identity.to_vec()));
            }
        }
        result.sort();
        result.dedup();
        result
    }

    pub fn archive_member_conflicts(&self, mod_name: &str, source_rel: &[u8]) -> Vec<(String, i8)> {
        let mod_key = mod_name.to_lowercase();
        let mut result = BTreeMap::new();
        for candidate in self.candidates.iter().filter(|candidate| {
            candidate.kind == ProviderKind::ArchiveMember
                && candidate.mod_key.as_ref() == mod_key
                && candidate.source_rel.as_ref() == source_rel
                && self.enabled(candidate)
                && self.selected(candidate)
        }) {
            let effective = self
                .inventory
                .effective(&candidate.target, &candidate.destination_key)
                .expect("candidate destination is interned");
            let status = self.destination_states.get(&effective).map_or(0, |state| {
                let edges = destination_edges(state, self);
                if edges.iter().any(|edge| edge.loser == candidate.mod_id) {
                    -1
                } else if edges.iter().any(|edge| edge.winner == candidate.mod_id) {
                    1
                } else {
                    0
                }
            });
            if status != 0 {
                result.insert(candidate.legacy_rel.to_string(), status);
            }
        }
        result.into_iter().collect()
    }

    pub fn deployment_plan(&self) -> DeploymentPlanRecord {
        let mut entries = Vec::new();
        for candidate_id in self.winners.values() {
            if let Some(entry) = self.deployment_entry(*candidate_id) {
                entries.push(entry);
            }
        }
        entries.sort_by_key(|entry| entry.candidate_id);
        entries.dedup_by_key(|entry| entry.candidate_id);
        entries.sort_by(|left, right| {
            (&left.target, &left.destination_key, left.candidate_id).cmp(&(
                &right.target,
                &right.destination_key,
                right.candidate_id,
            ))
        });
        DeploymentPlanRecord {
            generation: self.generation,
            inventory_generation: self.inventory_generation,
            entries,
        }
    }

    pub fn deployment_matches(&self, previous: &[DeployedStateRecord], link_mode: &str) -> bool {
        let mut seen = HashSet::new();
        let mut current: Vec<_> = self
            .winners
            .values()
            .filter_map(|candidate_id| {
                if !seen.insert(*candidate_id) {
                    return None;
                }
                self.deployment_candidate(*candidate_id)
            })
            .collect();
        if current.len() != previous.len() {
            return false;
        }
        current.sort_by(|left, right| {
            (&left.target, &left.destination_key, left.id).cmp(&(
                &right.target,
                &right.destination_key,
                right.id,
            ))
        });
        current.iter().zip(previous).all(|(candidate, deployed)| {
            let mut fingerprint = [0_u8; 16];
            fingerprint[..8].copy_from_slice(&candidate.size.to_le_bytes());
            fingerprint[8..].copy_from_slice(&candidate.mtime_ns.to_le_bytes());
            deployed.target == candidate.target.as_ref()
                && deployed.destination_key.as_slice() == candidate.destination_key.as_ref()
                && deployed.destination_display == self.casing.apply(candidate)
                && deployed.mod_key == candidate.mod_key.as_ref()
                && deployed.provider_kind == candidate.kind
                && deployed.source_rel.as_slice() == candidate.source_rel.as_ref()
                && deployed.source_display == candidate.source_display.as_ref()
                && deployed.source_fingerprint == fingerprint
                && deployed.link_mode.eq_ignore_ascii_case(link_mode)
        })
    }

    fn deployment_candidate(&self, candidate_id: i64) -> Option<&Candidate> {
        let candidate = self.candidate(candidate_id)?;
        let namespace = candidate.kind.namespace();
        if namespace == Namespace::Archive || !candidate.deployable {
            return None;
        }
        let effective = self
            .inventory
            .effective(&candidate.target, &candidate.destination_key)?;
        let key = PathKey {
            namespace,
            effective,
        };
        if self.winners.get(&key) != Some(&candidate_id) {
            return None;
        }
        if namespace == Namespace::Normal
            && self.winners.contains_key(&PathKey {
                namespace: Namespace::Root,
                effective,
            })
        {
            return None;
        }
        Some(candidate)
    }

    fn deployment_entry(&self, candidate_id: i64) -> Option<DeployEntryRecord> {
        let candidate = self.deployment_candidate(candidate_id)?;
        Some(DeployEntryRecord {
            candidate_id: candidate.id,
            mod_name: candidate.mod_name.to_string(),
            mod_key: candidate.mod_key.to_string(),
            provider_kind: candidate.kind,
            target: candidate.target.to_string(),
            destination_key: candidate.destination_key.to_vec(),
            destination_display: self.casing.apply(candidate),
            source_rel: candidate.source_rel.to_vec(),
            source_display: candidate.source_display.to_string(),
            source_fingerprint: [
                candidate.size.to_le_bytes().as_slice(),
                candidate.mtime_ns.to_le_bytes().as_slice(),
            ]
            .concat(),
            legacy_root: candidate.legacy_root,
            legacy_rel: candidate.legacy_rel.to_string(),
            flags: candidate.flags,
        })
    }

    pub fn deployment_entries(&self, candidate_ids: &BTreeSet<i64>) -> Vec<DeployEntryRecord> {
        let mut entries: Vec<_> = candidate_ids
            .iter()
            .filter_map(|candidate_id| self.deployment_entry(*candidate_id))
            .collect();
        entries.sort_by(|left, right| {
            (&left.target, &left.destination_key, left.candidate_id).cmp(&(
                &right.target,
                &right.destination_key,
                right.candidate_id,
            ))
        });
        entries
    }

    pub fn contested_winner_ids(&self, candidate_ids: &BTreeSet<i64>) -> Vec<i64> {
        candidate_ids
            .iter()
            .copied()
            .filter(|candidate_id| self.is_contested(*candidate_id))
            .collect()
    }

    fn is_contested(&self, candidate_id: i64) -> bool {
        let Some(candidate) = self.candidate(candidate_id) else {
            return false;
        };
        let Some(effective) = self
            .inventory
            .effective(&candidate.target, &candidate.destination_key)
        else {
            return false;
        };
        let Some(state) = self.destination_states.get(&effective) else {
            return false;
        };
        let mut first_owner = None;
        for provider in state
            .providers
            .iter()
            .filter_map(|index| self.candidate_at(*index))
        {
            match first_owner {
                None => first_owner = Some(provider.mod_key.as_ref()),
                Some(owner) if owner != provider.mod_key.as_ref() => return true,
                Some(_) => {}
            }
        }
        false
    }

    pub fn data_entries(&self) -> Vec<DataEntryRecord> {
        let mut candidate_ids: Vec<_> = self.winners.values().copied().collect();
        candidate_ids.sort_unstable();
        candidate_ids.dedup();
        let mut entries: Vec<_> = candidate_ids
            .into_iter()
            .filter_map(|candidate_id| {
                let candidate = self.deployment_candidate(candidate_id)?;
                Some(DataEntryRecord {
                    candidate_id,
                    mod_name: candidate.mod_name.to_string(),
                    target: candidate.target.to_string(),
                    destination_display: self.casing.apply(candidate),
                    contested: self.is_contested(candidate_id),
                })
            })
            .collect();
        entries.sort_by(|left, right| {
            (&left.target, &left.destination_display, left.candidate_id).cmp(&(
                &right.target,
                &right.destination_display,
                right.candidate_id,
            ))
        });
        entries
    }

    fn edge_record(&self, edge: &EdgeKey, refcount: u64) -> Option<ConflictEdgeRecord> {
        Some(ConflictEdgeRecord {
            kind: edge.kind.as_str().to_owned(),
            loser: self.inventory.mod_name(edge.loser)?.to_owned(),
            winner: self.inventory.mod_name(edge.winner)?.to_owned(),
            refcount,
        })
    }

    fn winner_record(&self, candidate: &Candidate, namespace: Namespace) -> WinnerRecord {
        let destination_display = self.casing.apply(candidate);
        WinnerRecord {
            candidate_id: candidate.id,
            mod_name: candidate.mod_name.to_string(),
            mod_key: candidate.mod_key.to_string(),
            target: candidate.target.to_string(),
            destination_key: candidate.destination_key.to_vec(),
            legacy_rel: self.casing.legacy(candidate, &destination_display),
            destination_display,
            source_rel: candidate.source_rel.to_vec(),
            source_display: candidate.source_display.to_string(),
            namespace,
            legacy_root: candidate.legacy_root,
            flags: candidate.flags,
        }
    }
}

fn kind_counter_mut(counter: &mut ModCounters, kind: CounterKind) -> &mut KindCounter {
    match kind {
        CounterKind::Loose => &mut counter.loose,
        CounterKind::Archive => &mut counter.archive,
        CounterKind::Identity => &mut counter.identity,
    }
}

fn signed_counter(value: u32, add: bool) -> i64 {
    if value == u32::MAX {
        if add { -1 } else { 1 }
    } else if add {
        i64::from(value)
    } else {
        -i64::from(value)
    }
}

fn queue_contribution(
    changes: &mut HashMap<(i64, CounterKind), CounterAccum>,
    delta: &CounterDelta,
    add: bool,
    affected: &mut HashSet<i64>,
) {
    affected.insert(delta.mod_id);
    let change = changes.entry((delta.mod_id, delta.kind)).or_default();
    change.wins += signed_counter(delta.wins, add);
    change.losses += signed_counter(delta.losses, add);
    change.surviving += signed_counter(delta.surviving, add);
    change.files += signed_counter(delta.files, add);
    for bit in 0..32 {
        if delta.flags & (1_u32 << bit) == 0 {
            continue;
        }
        change.flags[bit] += if add { 1 } else { -1 };
    }
}

fn adjust_signed(counter: &mut u64, delta: i64) {
    if delta >= 0 {
        *counter = counter.saturating_add(delta as u64);
    } else {
        *counter = counter.saturating_sub(delta.unsigned_abs());
    }
}

fn apply_counter_deltas(
    counters: &mut HashMap<i64, ModCounters>,
    changes: HashMap<(i64, CounterKind), CounterAccum>,
) {
    for ((mod_id, kind), change) in changes {
        let counter = counters.entry(mod_id).or_default();
        let kind_counter = kind_counter_mut(counter, kind);
        adjust_signed(&mut kind_counter.wins, change.wins);
        adjust_signed(&mut kind_counter.losses, change.losses);
        adjust_signed(&mut kind_counter.surviving, change.surviving);
        adjust_signed(&mut kind_counter.files, change.files);
        for (count, delta) in counter.flag_counts.iter_mut().zip(change.flags) {
            if delta >= 0 {
                *count = count.saturating_add(delta as u32);
            } else {
                *count = count.saturating_sub(delta.unsigned_abs() as u32);
            }
        }
    }
}

fn queue_edge(deltas: &mut HashMap<EdgeKey, i64>, edge: EdgeKey, add: bool) {
    let delta = deltas.entry(edge).or_default();
    *delta += if add { 1 } else { -1 };
}

fn apply_edge_deltas(
    edges: &mut PersistentHashMap<EdgeKey, u64>,
    deltas: HashMap<EdgeKey, i64>,
) -> HashSet<EdgeKey> {
    let mut changed = HashSet::with_capacity(deltas.len());
    for (edge, delta) in deltas {
        if delta == 0 {
            continue;
        }
        let before = edges.get(&edge).copied().unwrap_or(0);
        let after = if delta > 0 {
            before.saturating_add(delta as u64)
        } else {
            before.saturating_sub(delta.unsigned_abs())
        };
        if after == before {
            continue;
        }
        changed.insert(edge);
        if after == 0 {
            edges.remove(&edge);
        } else {
            edges.insert(edge, after);
        }
    }
    changed
}

fn apply_edge_effect(
    kind: ConflictKind,
    loser_mod_id: i64,
    winner_mod_id: i64,
    add: bool,
    affected: &mut HashSet<i64>,
    counter_deltas: &mut HashMap<(i64, CounterKind), CounterAccum>,
    edge_deltas: &mut HashMap<EdgeKey, i64>,
) {
    if loser_mod_id == winner_mod_id {
        return;
    }
    queue_edge(
        edge_deltas,
        EdgeKey {
            kind,
            loser: loser_mod_id,
            winner: winner_mod_id,
        },
        add,
    );
    let (loser_kind, winner_kind) = match kind {
        ConflictKind::Archive => (CounterKind::Archive, CounterKind::Archive),
        ConflictKind::Identity => (CounterKind::Identity, CounterKind::Identity),
        ConflictKind::LooseArchive => (CounterKind::Archive, CounterKind::Loose),
        _ => (CounterKind::Loose, CounterKind::Loose),
    };
    queue_contribution(
        counter_deltas,
        &CounterDelta::edge_loser(loser_mod_id, loser_kind),
        add,
        affected,
    );
    queue_contribution(
        counter_deltas,
        &CounterDelta::edge_winner(winner_mod_id, winner_kind),
        add,
        affected,
    );
}

fn edge_key(kind: ConflictKind, loser: &Candidate, winner: &Candidate) -> Option<EdgeKey> {
    (loser.mod_id != winner.mod_id).then_some(EdgeKey {
        kind,
        loser: loser.mod_id,
        winner: winner.mod_id,
    })
}

fn disabled(intent: &ProfileIntent, candidate: &Candidate) -> bool {
    if candidate.legacy_rel.contains('/') || candidate.legacy_rel.contains('\\') {
        return false;
    }
    intent
        .disabled_plugin_paths
        .get(candidate.mod_key.as_ref())
        .is_some_and(|paths| paths.contains(&candidate.legacy_rel.to_lowercase().into_bytes()))
}

fn build_identity_state(
    identity: &[u8],
    snapshot: &GraphSnapshot,
    ranks: &RankContext,
) -> IdentityState {
    let mut indexes: SmallVec<[ProviderIndex; 4]> = snapshot
        .inventory
        .identity_postings
        .get(identity)
        .into_iter()
        .flatten()
        .copied()
        .filter(|index| {
            let candidate = &snapshot.candidates[*index as usize];
            snapshot
                .inventory
                .active_candidate_ids
                .contains(&candidate.id)
                && ranks.active(candidate)
        })
        .collect();
    indexes.sort_unstable_by_key(|index| ranks.rank(&snapshot.candidates[*index as usize]));
    IdentityState { providers: indexes }
}

fn namespace_mod_flags(
    ids: &[ProviderIndex],
    snapshot: &GraphSnapshot,
) -> SmallVec<[(i64, u32); 4]> {
    let mut flags_by_mod: SmallVec<[(i64, u32); 4]> = SmallVec::new();
    for id in ids {
        if let Some(candidate) = snapshot.candidate_at(*id) {
            if let Some((_, flags)) = flags_by_mod
                .iter_mut()
                .find(|(mod_id, _)| *mod_id == candidate.mod_id)
            {
                *flags |= candidate.flags;
            } else {
                flags_by_mod.push((candidate.mod_id, candidate.flags));
            }
        }
    }
    flags_by_mod
}

fn namespace_edges(
    ids: &[ProviderIndex],
    kind: ConflictKind,
    snapshot: &GraphSnapshot,
    result: &mut SmallVec<[EdgeKey; 4]>,
) {
    let mut previous_mod = None;
    for id in ids {
        let Some(mod_id) = snapshot.candidate_at(*id).map(|candidate| candidate.mod_id) else {
            continue;
        };
        if previous_mod == Some(mod_id) {
            continue;
        }
        if let Some(loser) = previous_mod {
            result.push(EdgeKey {
                kind,
                loser,
                winner: mod_id,
            });
        }
        previous_mod = Some(mod_id);
    }
}

fn destination_edges(state: &DestinationState, snapshot: &GraphSnapshot) -> SmallVec<[EdgeKey; 4]> {
    let mut result = SmallVec::new();
    namespace_edges(
        state.stack(Namespace::Normal),
        ConflictKind::Loose,
        snapshot,
        &mut result,
    );
    namespace_edges(
        state.stack(Namespace::Root),
        ConflictKind::Loose,
        snapshot,
        &mut result,
    );
    namespace_edges(
        state.stack(Namespace::Archive),
        ConflictKind::Archive,
        snapshot,
        &mut result,
    );
    if let (Some(normal_id), Some(root_id)) = (
        state.published(Namespace::Normal),
        state.published(Namespace::Root),
    ) && let (Some(normal), Some(root)) = (
        snapshot.candidate_at(normal_id),
        snapshot.candidate_at(root_id),
    ) && let Some(edge) = edge_key(ConflictKind::Loose, normal, root)
    {
        result.push(edge);
    }
    if state.loose_archive_conflict
        && let (Some(archive_id), Some(loose_id)) = (
            state.published(Namespace::Archive),
            state.published(Namespace::Normal),
        )
        && let (Some(archive), Some(loose)) = (
            snapshot.candidate_at(archive_id),
            snapshot.candidate_at(loose_id),
        )
        && let Some(edge) = edge_key(ConflictKind::LooseArchive, archive, loose)
    {
        result.push(edge);
    }
    result
}

fn identity_edges(state: &IdentityState, snapshot: &GraphSnapshot) -> SmallVec<[EdgeKey; 4]> {
    let mut result = SmallVec::new();
    for pair in state.providers.windows(2) {
        if let (Some(loser), Some(winner)) = (
            snapshot.candidate_at(pair[0]),
            snapshot.candidate_at(pair[1]),
        ) && let Some(edge) = edge_key(ConflictKind::Identity, loser, winner)
        {
            result.push(edge);
        }
    }
    result
}

fn apply_destination_delta(
    snapshot: &mut GraphSnapshot,
    old: Option<&DestinationState>,
    new: &DestinationState,
    affected: &mut HashSet<i64>,
    counter_deltas: &mut HashMap<(i64, CounterKind), CounterAccum>,
    edge_deltas: &mut HashMap<EdgeKey, i64>,
) {
    for namespace in [Namespace::Normal, Namespace::Root, Namespace::Archive] {
        let old_ids = old.map_or(&[][..], |state| state.stack(namespace));
        let new_ids = new.stack(namespace);
        let old_flags = namespace_mod_flags(old_ids, snapshot);
        let new_flags = namespace_mod_flags(new_ids, snapshot);
        let kind = if namespace == Namespace::Archive {
            CounterKind::Archive
        } else {
            CounterKind::Loose
        };
        for &(mod_id, flags) in &old_flags {
            if !new_flags.contains(&(mod_id, flags)) {
                queue_contribution(
                    counter_deltas,
                    &CounterDelta::files(mod_id, kind, flags),
                    false,
                    affected,
                );
            }
        }
        for &(mod_id, flags) in &new_flags {
            if !old_flags.contains(&(mod_id, flags)) {
                queue_contribution(
                    counter_deltas,
                    &CounterDelta::files(mod_id, kind, flags),
                    true,
                    affected,
                );
            }
        }
        let old_winner = old_ids
            .last()
            .and_then(|id| snapshot.candidate_at(*id))
            .map(|candidate| candidate.mod_id);
        let new_winner = new_ids
            .last()
            .and_then(|id| snapshot.candidate_at(*id))
            .map(|candidate| candidate.mod_id);
        if old_winner != new_winner {
            if let Some(mod_id) = old_winner {
                queue_contribution(
                    counter_deltas,
                    &CounterDelta::surviving(mod_id, kind),
                    false,
                    affected,
                );
            }
            if let Some(mod_id) = new_winner {
                queue_contribution(
                    counter_deltas,
                    &CounterDelta::surviving(mod_id, kind),
                    true,
                    affected,
                );
            }
        }
    }

    let mut new_edges = destination_edges(new, snapshot);
    for edge in old
        .map(|state| destination_edges(state, snapshot))
        .unwrap_or_default()
    {
        if let Some(index) = new_edges.iter().position(|candidate| *candidate == edge) {
            new_edges.remove(index);
            continue;
        }
        apply_edge_effect(
            edge.kind,
            edge.loser,
            edge.winner,
            false,
            affected,
            counter_deltas,
            edge_deltas,
        );
    }
    for edge in new_edges {
        apply_edge_effect(
            edge.kind,
            edge.loser,
            edge.winner,
            true,
            affected,
            counter_deltas,
            edge_deltas,
        );
    }

    let cross_suppressed = |state: Option<&DestinationState>| {
        let state = state?;
        let normal = snapshot.candidate_at(state.published(Namespace::Normal)?)?;
        let root = snapshot.candidate_at(state.published(Namespace::Root)?)?;
        (normal.mod_id != root.mod_id).then_some(normal.mod_id)
    };
    let old_cross = cross_suppressed(old);
    let new_cross = cross_suppressed(Some(new));
    if old_cross != new_cross {
        if let Some(mod_id) = old_cross {
            queue_contribution(
                counter_deltas,
                &CounterDelta::remove_surviving(mod_id, CounterKind::Loose),
                false,
                affected,
            );
        }
        if let Some(mod_id) = new_cross {
            queue_contribution(
                counter_deltas,
                &CounterDelta::remove_surviving(mod_id, CounterKind::Loose),
                true,
                affected,
            );
        }
    }

    let archive_suppressed = |state: Option<&DestinationState>| {
        let state = state?;
        if !state.loose_archive_conflict {
            return None;
        }
        snapshot
            .candidate_at(state.published(Namespace::Archive)?)
            .map(|candidate| candidate.mod_id)
    };
    let old_archive = archive_suppressed(old);
    let new_archive = archive_suppressed(Some(new));
    if old_archive != new_archive {
        if let Some(mod_id) = old_archive {
            queue_contribution(
                counter_deltas,
                &CounterDelta::remove_surviving(mod_id, CounterKind::Archive),
                false,
                affected,
            );
        }
        if let Some(mod_id) = new_archive {
            queue_contribution(
                counter_deltas,
                &CounterDelta::remove_surviving(mod_id, CounterKind::Archive),
                true,
                affected,
            );
        }
    }
}

fn apply_identity_effects(
    snapshot: &mut GraphSnapshot,
    state: &IdentityState,
    add: bool,
    affected: &mut HashSet<i64>,
    counter_deltas: &mut HashMap<(i64, CounterKind), CounterAccum>,
    edge_deltas: &mut HashMap<EdgeKey, i64>,
) {
    let mut seen_mods: SmallVec<[i64; 4]> = SmallVec::new();
    for candidate_id in &state.providers {
        let Some(mod_id) = snapshot
            .candidate_at(*candidate_id)
            .map(|candidate| candidate.mod_id)
        else {
            continue;
        };
        if seen_mods.contains(&mod_id) {
            continue;
        }
        seen_mods.push(mod_id);
        queue_contribution(
            counter_deltas,
            &CounterDelta::files(mod_id, CounterKind::Identity, 0),
            add,
            affected,
        );
    }
    if let Some(winner) = state.providers.last()
        && let Some(mod_id) = snapshot
            .candidate_at(*winner)
            .map(|candidate| candidate.mod_id)
    {
        queue_contribution(
            counter_deltas,
            &CounterDelta::surviving(mod_id, CounterKind::Identity),
            add,
            affected,
        );
    }
    for edge in identity_edges(state, snapshot) {
        apply_edge_effect(
            edge.kind,
            edge.loser,
            edge.winner,
            add,
            affected,
            counter_deltas,
            edge_deltas,
        );
    }
}

fn build_destination_state(
    effective: &EffectiveKey,
    snapshot: &GraphSnapshot,
    intent: &ProfileIntent,
    ranks: &RankContext,
) -> DestinationState {
    let mut normal: SmallVec<[ProviderIndex; 4]> = SmallVec::new();
    let mut root: SmallVec<[ProviderIndex; 4]> = SmallVec::new();
    let mut archive: SmallVec<[ProviderIndex; 4]> = SmallVec::new();
    for index in snapshot
        .inventory
        .effective_postings
        .get(*effective as usize)
        .into_iter()
        .flatten()
        .copied()
    {
        let candidate = &snapshot.candidates[index as usize];
        if !snapshot
            .inventory
            .active_candidate_ids
            .contains(&candidate.id)
            || !ranks.active(candidate)
        {
            continue;
        }
        let namespace = candidate.kind.namespace();
        if namespace != Namespace::Archive
            && (!candidate.deployable
                || disabled(intent, candidate)
                || snapshot
                    .suppressed_counts
                    .get(&candidate.id)
                    .copied()
                    .unwrap_or(0)
                    != 0)
        {
            continue;
        }
        match namespace {
            Namespace::Normal => normal.push(index),
            Namespace::Root => root.push(index),
            Namespace::Archive => archive.push(index),
        }
    }
    let rank = |index: &ProviderIndex| ranks.rank(&snapshot.candidates[*index as usize]);
    normal.sort_unstable_by_key(rank);
    root.sort_unstable_by_key(rank);
    archive.sort_unstable_by_key(rank);
    let suppressed = |index: ProviderIndex| {
        snapshot
            .candidate_at(index)
            .and_then(|candidate| snapshot.suppressed_counts.get(&candidate.id).copied())
            .unwrap_or(0)
            > 0
    };
    let loose_archive_conflict = intent.loose_beats_archive
        && archive
            .last()
            .zip(normal.last())
            .is_some_and(|(archive_id, loose_id)| {
                let archive = snapshot.candidate_at(*archive_id).unwrap();
                let loose = snapshot.candidate_at(*loose_id).unwrap();
                archive.mod_id != loose.mod_id && !suppressed(*loose_id)
            });
    let normal_end = normal.len() as u32;
    let root_end = normal_end.saturating_add(root.len() as u32);
    let mut providers = SmallVec::with_capacity(normal.len() + root.len() + archive.len());
    providers.extend(normal);
    providers.extend(root);
    providers.extend(archive);
    DestinationState {
        providers,
        normal_end,
        root_end,
        loose_archive_conflict,
    }
}

fn status_code(counter: &KindCounter) -> i8 {
    match (counter.wins > 0, counter.losses > 0) {
        (false, false) => 0,
        (true, false) => 1,
        (true, true) => 2,
        (false, true) if counter.surviving == 0 && counter.files > 0 => 3,
        (false, true) => -1,
    }
}

fn summary(counter: &ModCounters) -> ConflictSummary {
    ConflictSummary {
        loose_code: status_code(&counter.loose),
        archive_code: status_code(&counter.archive),
        identity_code: status_code(&counter.identity),
        loose_wins: counter.loose.wins,
        loose_losses: counter.loose.losses,
        loose_surviving: counter.loose.surviving,
        archive_wins: counter.archive.wins,
        archive_losses: counter.archive.losses,
        archive_surviving: counter.archive.surviving,
        identity_wins: counter.identity.wins,
        identity_losses: counter.identity.losses,
        flags: counter.flags(),
    }
}

fn plugin_candidate(snapshot: &GraphSnapshot, plugin: &str) -> Option<(PathKey, i64)> {
    let paths = snapshot.inventory.plugin_paths.get(plugin)?;
    let mut selected: Option<(u8, i64, PathKey)> = None;
    for path in paths {
        let Some(candidate_id) = snapshot.winners.get(path).copied() else {
            continue;
        };
        let phase = u8::from(path.namespace == Namespace::Root);
        if selected
            .as_ref()
            .is_none_or(|current| (phase, candidate_id) > (current.0, current.1))
        {
            selected = Some((phase, candidate_id, path.clone()));
        }
    }
    let (_, candidate_id, path) = selected?;
    Some((path, candidate_id))
}

fn plugin_owner(snapshot: &GraphSnapshot, plugin: &str) -> Option<String> {
    snapshot
        .candidate(plugin_candidate(snapshot, plugin)?.1)
        .map(|candidate| candidate.mod_name.to_string())
}

fn rebuild_plugin_owners(snapshot: &GraphSnapshot) -> BTreeMap<String, String> {
    let mut result = BTreeMap::new();
    for plugin in snapshot.inventory.plugin_paths.keys() {
        if let Some(owner) = plugin_owner(snapshot, plugin) {
            result.insert(plugin.clone(), owner);
        }
    }
    result
}

fn refresh_plugin_owners(
    snapshot: &mut GraphSnapshot,
    _previous: &GraphSnapshot,
    changed_winners: &HashSet<PathKey>,
) {
    let mut dirty = BTreeSet::new();
    // Plugin paths are sparse (hundreds) while a patcher toggle may change
    // tens of thousands of ordinary assets.  Intersect from the sparse side
    // instead of probing both snapshots/candidates for every changed texture.
    for (plugin, paths) in &snapshot.inventory.plugin_paths {
        if paths.iter().any(|path| changed_winners.contains(path)) {
            dirty.insert(plugin.clone());
        }
    }
    for plugin in dirty {
        if let Some(owner) = plugin_owner(snapshot, &plugin) {
            snapshot.plugin_owners.insert(plugin, owner);
        } else {
            snapshot.plugin_owners.remove(&plugin);
        }
    }
}

fn rebuild_casing(snapshot: &mut GraphSnapshot, intent: &ProfileIntent) {
    let mut casing = CasingState::from_intent(intent);
    if !casing.normalize {
        snapshot.casing = casing;
        return;
    }

    // Full restoration is a bulk construction, not an incremental edit. Build
    // mutable standard maps once and convert them to persistent structures at
    // publication; CasingState::add intentionally copy-on-writes each context
    // and posting set, which is ideal for small deltas but quadratic here.
    let winners: Vec<_> = snapshot
        .winners
        .iter()
        .filter_map(|(key, candidate_id)| {
            (key.namespace != Namespace::Archive)
                .then(|| snapshot.candidate(*candidate_id))
                .flatten()
                .map(|candidate| (key.clone(), candidate.clone()))
        })
        .collect();
    let mut variants: HashMap<CasingContext, BTreeMap<String, CasingVariant>> = HashMap::new();
    let mut postings: HashMap<CasingContext, HashSet<PathKey>> = HashMap::new();
    for (key, candidate) in winners {
        for (context, spelling) in CasingState::candidate_contexts(&candidate) {
            let value = variants
                .entry(context.clone())
                .or_default()
                .entry(spelling)
                .or_insert(CasingVariant {
                    count: 0,
                    first_candidate_id: candidate.id,
                });
            value.count = value.count.saturating_add(1);
            value.first_candidate_id = value.first_candidate_id.min(candidate.id);
            postings.entry(context).or_default().insert(key.clone());
        }
    }
    casing.variants = variants
        .into_iter()
        .map(|(context, values)| (context, Arc::new(values)))
        .collect();
    casing.postings = postings
        .into_iter()
        .map(|(context, paths)| (context, paths.into_iter().collect()))
        .collect();
    casing.canonical = casing
        .variants
        .iter()
        .filter_map(|(context, values)| {
            casing
                .pick(values)
                .map(|selected| (context.clone(), selected))
        })
        .collect();
    snapshot.casing = casing;
}

fn refresh_casing(
    snapshot: &mut GraphSnapshot,
    previous: &GraphSnapshot,
    changed_winners: &HashSet<PathKey>,
) -> HashSet<PathKey> {
    if !snapshot.casing.normalize {
        return HashSet::new();
    }
    // Updating a patcher-sized set one winner at a time repeatedly publishes
    // the same directory context and copy-on-write postings set. Group all
    // changes for a context and publish it once; operation order within each
    // context stays identical to the scalar path below.
    if changed_winners.len() >= 512 {
        let mut operations: HashMap<CasingContext, Vec<(String, i64, PathKey, bool)>> =
            HashMap::new();
        for key in changed_winners {
            if let Some(candidate) = previous
                .winners
                .get(key)
                .and_then(|candidate_id| previous.candidate(*candidate_id))
            {
                for (context, spelling) in CasingState::candidate_contexts(candidate) {
                    operations.entry(context).or_default().push((
                        spelling,
                        candidate.id,
                        key.clone(),
                        false,
                    ));
                }
            }
            if let Some(candidate) = snapshot
                .winners
                .get(key)
                .and_then(|candidate_id| snapshot.candidate(*candidate_id))
            {
                for (context, spelling) in CasingState::candidate_contexts(candidate) {
                    operations.entry(context).or_default().push((
                        spelling,
                        candidate.id,
                        key.clone(),
                        true,
                    ));
                }
            }
        }

        let mut result = HashSet::new();
        for (context, context_operations) in operations {
            let before = snapshot.casing.canonical.get(&context).cloned();
            let mut variants = snapshot
                .casing
                .variants
                .get(&context)
                .map(|values| (**values).clone())
                .unwrap_or_default();
            let mut postings = snapshot
                .casing
                .postings
                .get(&context)
                .cloned()
                .unwrap_or_default();
            for (spelling, candidate_id, key, add) in context_operations {
                if add {
                    let value = variants.entry(spelling).or_insert(CasingVariant {
                        count: 0,
                        first_candidate_id: candidate_id,
                    });
                    value.count = value.count.saturating_add(1);
                    value.first_candidate_id = value.first_candidate_id.min(candidate_id);
                    postings.insert(key);
                } else {
                    if let Some(value) = variants.get_mut(&spelling) {
                        value.count = value.count.saturating_sub(1);
                        if value.count == 0 {
                            variants.remove(&spelling);
                        }
                    }
                    postings.remove(&key);
                }
            }
            let selected = snapshot.casing.pick(&variants);
            if variants.is_empty() {
                snapshot.casing.variants.remove(&context);
            } else {
                snapshot
                    .casing
                    .variants
                    .insert(context.clone(), Arc::new(variants));
            }
            if postings.is_empty() {
                snapshot.casing.postings.remove(&context);
            } else {
                snapshot.casing.postings.insert(context.clone(), postings);
            }
            if let Some(selected) = selected {
                snapshot.casing.canonical.insert(context.clone(), selected);
            } else {
                snapshot.casing.canonical.remove(&context);
            }
            if before != snapshot.casing.canonical.get(&context).cloned() {
                if let Some(paths) = previous.casing.postings.get(&context) {
                    result.extend(paths.iter().cloned());
                }
                if let Some(paths) = snapshot.casing.postings.get(&context) {
                    result.extend(paths.iter().cloned());
                }
            }
        }
        return result;
    }
    let mut contexts = HashSet::new();
    for key in changed_winners {
        if let Some(candidate) = previous
            .winners
            .get(key)
            .and_then(|candidate_id| previous.candidate(*candidate_id))
        {
            contexts.extend(snapshot.casing.remove(key, candidate));
        }
        if let Some(candidate) = snapshot
            .winners
            .get(key)
            .and_then(|candidate_id| snapshot.candidate(*candidate_id))
            .cloned()
        {
            contexts.extend(snapshot.casing.add(key, &candidate));
        }
    }
    let mut result = HashSet::new();
    for context in contexts {
        if let Some(paths) = previous.casing.postings.get(&context) {
            result.extend(paths.iter().cloned());
        }
        if let Some(paths) = snapshot.casing.postings.get(&context) {
            result.extend(paths.iter().cloned());
        }
    }
    result
}

fn deployed_keys(candidate: &Candidate) -> Option<(Vec<u8>, Vec<u8>)> {
    if !candidate.deployable || candidate.kind == ProviderKind::ArchiveMember {
        return None;
    }
    let path = candidate
        .legacy_rel
        .replace('\\', "/")
        .to_lowercase()
        .into_bytes();
    let basename = path
        .rsplit(|byte| *byte == b'/')
        .next()
        .unwrap_or(&path)
        .to_vec();
    Some((path, basename))
}

fn adjust_deployed_key(values: &mut PersistentHashMap<Vec<u8>, u32>, key: Vec<u8>, add: bool) {
    let count = values.get(&key).copied().unwrap_or(0);
    if add {
        values.insert(key, count.saturating_add(1));
    } else if count <= 1 {
        values.remove(&key);
    } else {
        values.insert(key, count - 1);
    }
}

fn rebuild_deployed_indexes(snapshot: &mut GraphSnapshot) {
    snapshot.deployed_paths.clear();
    snapshot.deployed_basenames.clear();
    let keys: Vec<_> = snapshot
        .winners
        .iter()
        .filter_map(|(path, id)| {
            (path.namespace != Namespace::Archive)
                .then(|| snapshot.candidate(*id))
                .flatten()
                .and_then(deployed_keys)
        })
        .collect();
    for (path, basename) in keys {
        adjust_deployed_key(&mut snapshot.deployed_paths, path, true);
        adjust_deployed_key(&mut snapshot.deployed_basenames, basename, true);
    }
}

fn refresh_deployed_indexes(
    snapshot: &mut GraphSnapshot,
    previous: &GraphSnapshot,
    changed_winners: &HashSet<PathKey>,
) {
    for key in changed_winners {
        if key.namespace == Namespace::Archive {
            continue;
        }
        let old_candidate = previous
            .winners
            .get(key)
            .and_then(|id| previous.candidate(*id));
        let new_candidate = snapshot
            .winners
            .get(key)
            .and_then(|id| snapshot.candidate(*id));
        if old_candidate.zip(new_candidate).is_some_and(|(old, new)| {
            old.deployable
                && new.deployable
                && old.kind != ProviderKind::ArchiveMember
                && new.kind != ProviderKind::ArchiveMember
        }) {
            continue;
        }
        let old_keys = old_candidate.and_then(deployed_keys);
        let new_keys = new_candidate.and_then(deployed_keys);
        // Most conflict changes replace the provider at the same destination.
        // The path/basename membership indexes are therefore unchanged and do
        // not need four persistent-map mutations per winner.
        if old_keys == new_keys {
            continue;
        }
        if let Some((path, basename)) = old_keys {
            adjust_deployed_key(&mut snapshot.deployed_paths, path, false);
            adjust_deployed_key(&mut snapshot.deployed_basenames, basename, false);
        }
        if let Some((path, basename)) = new_keys {
            adjust_deployed_key(&mut snapshot.deployed_paths, path, true);
            adjust_deployed_key(&mut snapshot.deployed_basenames, basename, true);
        }
    }
}

fn install_state(
    snapshot: &mut GraphSnapshot,
    effective: EffectiveKey,
    state: DestinationState,
    affected: &mut HashSet<i64>,
    counter_deltas: &mut HashMap<(i64, CounterKind), CounterAccum>,
    edge_deltas: &mut HashMap<EdgeKey, i64>,
    changed_winners: &mut HashSet<PathKey>,
    changed_deployed_indexes: &mut HashSet<PathKey>,
    touched_winner_ids: &mut Vec<i64>,
) {
    let old = snapshot.destination_states.get(&effective).cloned();
    apply_destination_delta(
        snapshot,
        old.as_deref(),
        &state,
        affected,
        counter_deltas,
        edge_deltas,
    );
    for namespace in [Namespace::Normal, Namespace::Root, Namespace::Archive] {
        let old_id = old
            .as_deref()
            .and_then(|value| value.published(namespace))
            .and_then(|index| snapshot.candidate_at(index))
            .map(|candidate| candidate.id);
        let new_id = state
            .published(namespace)
            .and_then(|index| snapshot.candidate_at(index))
            .map(|candidate| candidate.id);
        if let Some(id) = new_id {
            touched_winner_ids.push(id);
        }
        if old_id == new_id {
            continue;
        }
        let key = PathKey {
            namespace,
            effective,
        };
        if namespace != Namespace::Archive {
            let old_deployed = old_id
                .and_then(|id| snapshot.candidate(id))
                .filter(|candidate| {
                    candidate.deployable && candidate.kind != ProviderKind::ArchiveMember
                });
            let new_deployed = new_id
                .and_then(|id| snapshot.candidate(id))
                .filter(|candidate| {
                    candidate.deployable && candidate.kind != ProviderKind::ArchiveMember
                });
            let deployed_index_changed = match (old_deployed, new_deployed) {
                (Some(old), Some(new)) => !old.legacy_rel.eq_ignore_ascii_case(&new.legacy_rel),
                (None, None) => false,
                _ => true,
            };
            if deployed_index_changed {
                changed_deployed_indexes.insert(key.clone());
            }
        }
        if let Some(id) = new_id {
            snapshot.winners.insert(key.clone(), id);
        } else {
            snapshot.winners.remove(&key);
        }
        changed_winners.insert(key);
    }
    snapshot
        .destination_states
        .insert(effective, Arc::new(state));
}

fn apply_suppressed(
    counts: &mut PersistentHashMap<i64, u32>,
    providers: &[ProviderIndex],
    candidates: &[Candidate],
    add: bool,
) -> HashSet<i64> {
    let mut toggled = HashSet::new();
    for provider in providers {
        let Some(candidate) = candidates.get(*provider as usize) else {
            continue;
        };
        let id = candidate.id;
        let before = counts.get(&id).copied().unwrap_or(0);
        let after = if add {
            before.saturating_add(1)
        } else {
            before.saturating_sub(1)
        };
        if after == 0 {
            counts.remove(&id);
        } else {
            counts.insert(id, after);
        }
        if (before == 0) != (after == 0) {
            toggled.insert(id);
        }
    }
    toggled
}

fn install_identity_state(
    snapshot: &mut GraphSnapshot,
    identity: Arc<[u8]>,
    state: IdentityState,
    affected: &mut HashSet<i64>,
    counter_deltas: &mut HashMap<(i64, CounterKind), CounterAccum>,
    edge_deltas: &mut HashMap<EdgeKey, i64>,
) -> HashSet<i64> {
    let mut toggled = HashSet::new();
    if let Some(old) = snapshot.identity_states.get(&identity).cloned() {
        apply_identity_effects(snapshot, &old, false, affected, counter_deltas, edge_deltas);
        toggled.extend(apply_suppressed(
            &mut snapshot.suppressed_counts,
            old.suppressed(),
            &snapshot.candidates,
            false,
        ));
    }
    apply_identity_effects(
        snapshot,
        &state,
        true,
        affected,
        counter_deltas,
        edge_deltas,
    );
    toggled.extend(apply_suppressed(
        &mut snapshot.suppressed_counts,
        state.suppressed(),
        &snapshot.candidates,
        true,
    ));
    snapshot.identity_states.insert(identity, Arc::new(state));
    toggled
}

fn finish_summaries(
    snapshot: &mut GraphSnapshot,
    intent: &ProfileIntent,
    affected: &HashSet<i64>,
) -> BTreeSet<String> {
    let intent_names: HashMap<_, _> = intent
        .mods
        .iter()
        .filter_map(|entry| {
            snapshot
                .inventory
                .mod_ids_by_key
                .get(entry.key.as_str())
                .map(|mod_id| (*mod_id, entry.name.as_str()))
        })
        .collect();
    let active_ids: HashSet<_> = intent_names.keys().copied().collect();
    let mut changed = BTreeSet::new();
    for mod_id in affected {
        let name = intent_names
            .get(mod_id)
            .copied()
            .or_else(|| snapshot.inventory.mod_name(*mod_id))
            .unwrap_or("<unknown mod>")
            .to_owned();
        let is_overwrite = snapshot
            .inventory
            .mod_keys
            .get(mod_id)
            .is_some_and(|key| key.as_ref() == "[overwrite]");
        let value = if active_ids.contains(mod_id) || is_overwrite {
            snapshot
                .counters
                .get(mod_id)
                .map(summary)
                .unwrap_or_default()
        } else {
            ConflictSummary::default()
        };
        if snapshot.summaries.get(&name) != Some(&value) {
            changed.insert(name.clone());
            if active_ids.contains(mod_id) || is_overwrite {
                snapshot.summaries.insert(name, value);
            } else {
                snapshot.summaries.remove(&name);
            }
        }
    }
    for entry in &intent.mods {
        if !snapshot.summaries.contains_key(&entry.name) {
            snapshot
                .summaries
                .insert(entry.name.clone(), ConflictSummary::default());
            changed.insert(entry.name.clone());
        }
    }
    changed
}

pub fn build_full(
    candidates: Arc<Vec<Candidate>>,
    raw_files: Arc<Vec<RawCatalogFile>>,
    intent: &ProfileIntent,
    inventory_generation: u64,
    generation: u64,
) -> GraphSnapshot {
    let trace =
        crate::model::perftrace_enabled() || std::env::var_os("AMETHYST_FILEGRAPH_TRACE").is_some();
    let build_started = Instant::now();
    let inventory = Arc::new(GraphInventory::new(&candidates));
    let inventory_elapsed = build_started.elapsed();
    let mut capability_flags = BTreeMap::new();
    for raw in raw_files.iter() {
        *capability_flags
            .entry(raw.mod_name.to_string())
            .or_insert(0) |= raw.flags;
    }
    let mut snapshot = GraphSnapshot {
        generation,
        inventory_generation,
        candidates,
        raw_files,
        inventory,
        destination_states: PersistentHashMap::new(),
        identity_states: PersistentHashMap::new(),
        suppressed_counts: PersistentHashMap::new(),
        winners: PersistentHashMap::new(),
        counters: HashMap::new(),
        summaries: BTreeMap::new(),
        edges: PersistentHashMap::new(),
        plugin_owners: BTreeMap::new(),
        capability_flags,
        casing: CasingState::from_intent(intent),
        enabled_mods: Arc::new(
            intent
                .mods
                .iter()
                .filter(|entry| entry.enabled)
                .map(|entry| entry.key.clone())
                .collect(),
        ),
        selected_variants: Arc::new(
            intent
                .mods
                .iter()
                .map(|entry| (entry.key.clone(), entry.variant_key.clone()))
                .collect(),
        ),
        deployed_paths: PersistentHashMap::new(),
        deployed_basenames: PersistentHashMap::new(),
        loose_beats_archive: intent.loose_beats_archive,
        rules_hash: intent.rules_hash.clone(),
    };
    let ranks = RankContext::new(intent, &snapshot.inventory);
    let base_elapsed = build_started.elapsed();
    let identities: Vec<_> = snapshot
        .inventory
        .identity_postings
        .keys()
        .cloned()
        .collect();
    let mut affected = HashSet::new();
    let mut counter_deltas = HashMap::new();
    let mut edge_deltas = HashMap::new();
    for identity in identities {
        let state = build_identity_state(&identity, &snapshot, &ranks);
        install_identity_state(
            &mut snapshot,
            identity,
            state,
            &mut affected,
            &mut counter_deltas,
            &mut edge_deltas,
        );
    }
    let identities_elapsed = build_started.elapsed();
    let destinations: Vec<_> = (0..snapshot.inventory.effective_postings.len() as u32).collect();
    let mut changed_winners = HashSet::new();
    let mut ignored_deployed_indexes = HashSet::new();
    let mut ignored_touched_winners = Vec::new();
    for effective in destinations {
        let state = build_destination_state(&effective, &snapshot, intent, &ranks);
        install_state(
            &mut snapshot,
            effective,
            state,
            &mut affected,
            &mut counter_deltas,
            &mut edge_deltas,
            &mut changed_winners,
            &mut ignored_deployed_indexes,
            &mut ignored_touched_winners,
        );
    }
    apply_counter_deltas(&mut snapshot.counters, counter_deltas);
    apply_edge_deltas(&mut snapshot.edges, edge_deltas);
    let destinations_elapsed = build_started.elapsed();
    affected.extend(intent.mods.iter().filter_map(|entry| {
        snapshot
            .inventory
            .mod_ids_by_key
            .get(entry.key.as_str())
            .copied()
    }));
    finish_summaries(&mut snapshot, intent, &affected);
    let summaries_elapsed = build_started.elapsed();
    rebuild_casing(&mut snapshot, intent);
    let casing_elapsed = build_started.elapsed();
    rebuild_deployed_indexes(&mut snapshot);
    let deployed_elapsed = build_started.elapsed();
    snapshot.plugin_owners = rebuild_plugin_owners(&snapshot);
    if trace {
        let millis = |later: std::time::Duration, earlier: std::time::Duration| {
            (later - earlier).as_secs_f64() * 1_000.0
        };
        let done = build_started.elapsed();
        eprintln!(
            "[filegraph] graph candidates={} effective_paths={} \
             inventory_ms={:.3} base_ms={:.3} identities_ms={:.3} \
             destinations_ms={:.3} summaries_ms={:.3} casing_ms={:.3} \
             deployed_index_ms={:.3} plugin_owners_ms={:.3}",
            snapshot.candidates.len(),
            snapshot.inventory.effective_postings.len(),
            inventory_elapsed.as_secs_f64() * 1_000.0,
            millis(base_elapsed, inventory_elapsed),
            millis(identities_elapsed, base_elapsed),
            millis(destinations_elapsed, identities_elapsed),
            millis(summaries_elapsed, destinations_elapsed),
            millis(casing_elapsed, summaries_elapsed),
            millis(deployed_elapsed, casing_elapsed),
            millis(done, deployed_elapsed),
        );
    }
    snapshot
}

fn changed_mods(previous_intent: &ProfileIntent, intent: &ProfileIntent) -> HashSet<String> {
    let old: HashMap<_, _> = previous_intent
        .mods
        .iter()
        .enumerate()
        .map(|(index, entry)| (entry.key.as_str(), (index, entry)))
        .collect();
    let new: HashMap<_, _> = intent
        .mods
        .iter()
        .enumerate()
        .map(|(index, entry)| (entry.key.as_str(), (index, entry)))
        .collect();
    let hinted: HashSet<String> = intent
        .hint
        .mods
        .iter()
        .map(|name| name.to_lowercase())
        .collect();
    let local_order_hint = matches!(
        intent.hint.kind.as_str(),
        "move" | "move_block" | "toggle" | "enable" | "disable"
    ) && !hinted.is_empty();
    // Adding/removing an entry shifts every later absolute index without
    // changing the priority relationship among the mods which remain. Compare
    // positions in the common subsequence so a new top-priority mod dirties
    // itself, not the other 400 unchanged mods below it.
    let old_common: Vec<_> = previous_intent
        .mods
        .iter()
        .filter(|entry| new.contains_key(entry.key.as_str()))
        .map(|entry| entry.key.as_str())
        .collect();
    let new_common: Vec<_> = intent
        .mods
        .iter()
        .filter(|entry| old.contains_key(entry.key.as_str()))
        .map(|entry| entry.key.as_str())
        .collect();
    let common_order_changed = old_common != new_common;
    let old_common_rank: HashMap<_, _> = old_common
        .iter()
        .enumerate()
        .map(|(index, key)| (*key, index))
        .collect();
    let new_common_rank: HashMap<_, _> = new_common
        .iter()
        .enumerate()
        .map(|(index, key)| (*key, index))
        .collect();
    let mut result = hinted;
    for key in old.keys().chain(new.keys()) {
        match (old.get(key), new.get(key)) {
            (Some((_old_index, old_entry)), Some((_new_index, new_entry))) => {
                if old_entry.enabled != new_entry.enabled
                    || old_entry.variant_key != new_entry.variant_key
                    || (!local_order_hint
                        && common_order_changed
                        && old_common_rank.get(key) != new_common_rank.get(key))
                {
                    result.insert((*key).to_owned());
                }
            }
            _ => {
                result.insert((*key).to_owned());
            }
        }
    }
    let disabled_keys: HashSet<_> = previous_intent
        .disabled_plugin_paths
        .keys()
        .chain(intent.disabled_plugin_paths.keys())
        .collect();
    for key in disabled_keys {
        if previous_intent.disabled_plugin_paths.get(key) != intent.disabled_plugin_paths.get(key) {
            result.insert(key.clone());
        }
    }
    result
}

fn archive_rank_changes(
    previous: &ProfileIntent,
    intent: &ProfileIntent,
    candidates: &[Candidate],
) -> HashSet<String> {
    if matches!(intent.hint.kind.as_str(), "move" | "move_block") && !intent.hint.mods.is_empty() {
        // changed_mods() already dirties every loose/archive destination owned
        // by the moved mod.  Absolute archive-order indices also change for
        // every archive shifted around that mod, but their relative ordering
        // with each other does not; dirtying all of them turns a local move
        // into an almost-full archive rebuild on Bethesda profiles.
        return HashSet::new();
    }
    let old: HashMap<_, _> = previous
        .archive_order
        .iter()
        .enumerate()
        .map(|(index, key)| (key.to_lowercase(), index))
        .collect();
    let new: HashMap<_, _> = intent
        .archive_order
        .iter()
        .enumerate()
        .map(|(index, key)| (key.to_lowercase(), index))
        .collect();
    let mut changed: HashSet<String> = old
        .keys()
        .chain(new.keys())
        .filter(|key| old.get(*key) != new.get(*key))
        .cloned()
        .collect();
    let old_plugins: HashMap<_, _> = previous
        .plugin_order
        .iter()
        .enumerate()
        .map(|(index, key)| (key.to_lowercase(), index))
        .collect();
    let new_plugins: HashMap<_, _> = intent
        .plugin_order
        .iter()
        .enumerate()
        .map(|(index, key)| (key.to_lowercase(), index))
        .collect();
    let moved_plugins: HashSet<_> = old_plugins
        .keys()
        .chain(new_plugins.keys())
        .filter(|key| old_plugins.get(*key) != new_plugins.get(*key))
        .cloned()
        .collect();
    if !moved_plugins.is_empty() {
        changed.extend(candidates.iter().filter_map(|candidate| {
            let plugin = candidate.plugin_key.as_ref()?.to_lowercase();
            if !moved_plugins.contains(&plugin) {
                return None;
            }
            candidate.archive_key.as_ref().map(|key| key.to_lowercase())
        }));
    }
    changed
}

/// Merge a newly selected catalog projection into stable provider slots.
///
/// Destination and identity states store compact provider indexes. Replacing
/// the complete candidate Vec would renumber most of those indexes after a mod
/// add/remove and force a full rebuild. Retaining the old prefix keeps every
/// existing stack valid; candidates absent from `current` become inactive and
/// new candidates append. Only destinations owned by `changed_mods` are then
/// reconciled. Stale slots are disposable and can be compacted by a later cold
/// rebuild without affecting semantics.
fn merge_inventory_candidates(
    previous: &GraphSnapshot,
    current: &[Candidate],
) -> (Arc<Vec<Candidate>>, Arc<GraphInventory>, HashSet<String>) {
    let active_candidate_ids: HashSet<i64> = current.iter().map(|candidate| candidate.id).collect();
    let previous_active = &previous.inventory.active_candidate_ids;
    let mut changed_mods = HashSet::new();
    let mut merged = previous.candidates.as_ref().clone();

    for candidate in current {
        if let Some(index) = previous.inventory.candidate_indexes.get(&candidate.id) {
            let slot = &mut merged[*index as usize];
            if slot != candidate {
                changed_mods.insert(slot.mod_key.to_string());
                changed_mods.insert(candidate.mod_key.to_string());
                *slot = candidate.clone();
            }
        } else {
            changed_mods.insert(candidate.mod_key.to_string());
            merged.push(candidate.clone());
        }
    }
    for candidate_id in previous_active.difference(&active_candidate_ids) {
        if let Some(index) = previous.inventory.candidate_indexes.get(candidate_id)
            && let Some(candidate) = previous.candidates.get(*index as usize)
        {
            changed_mods.insert(candidate.mod_key.to_string());
        }
    }

    let inventory = Arc::new(GraphInventory::new_with_active(
        &merged,
        active_candidate_ids,
    ));
    (Arc::new(merged), inventory, changed_mods)
}

pub fn reconcile_graph(
    previous: &GraphSnapshot,
    previous_intent: Option<&ProfileIntent>,
    candidates: Arc<Vec<Candidate>>,
    raw_files: Arc<Vec<RawCatalogFile>>,
    intent: &ProfileIntent,
    inventory_generation: u64,
    generation: u64,
) -> GraphUpdate {
    let trace =
        crate::model::perftrace_enabled() || std::env::var_os("AMETHYST_FILEGRAPH_TRACE").is_some();
    let reconcile_started = Instant::now();
    let must_rebuild = previous_intent.is_none()
        || previous.rules_hash != intent.rules_hash
        || previous_intent.is_some_and(|old| {
            old.normalize_folder_case != intent.normalize_folder_case
                || old.casing_strategy != intent.casing_strategy
                || old.casing_pins != intent.casing_pins
        });
    if must_rebuild {
        let snapshot = build_full(
            candidates,
            raw_files,
            intent,
            inventory_generation,
            generation,
        );
        return update_from_full(previous, snapshot);
    }
    let previous_intent = previous_intent.unwrap();
    let mut snapshot = previous.clone();
    let selected_variants = Arc::new(
        intent
            .mods
            .iter()
            .map(|entry| (entry.key.clone(), entry.variant_key.clone()))
            .collect::<HashMap<_, _>>(),
    );
    let mut inventory_changed_mods = HashSet::new();
    // Inventory generation and route-variant selection are the authoritative
    // identity of the catalog projection.  Do not use Arc pointer equality
    // here: after an incremental add/remove, merge_inventory_candidates keeps
    // stable provider slots in a new Vec while load_candidates continues to
    // return the compact catalog-cache Vec.  Those allocations intentionally
    // differ, and treating that as another inventory change rebuilt (and later
    // freed) the complete GraphInventory on every subsequent toggle or move.
    if previous.inventory_generation != inventory_generation
        || previous.selected_variants.as_ref() != selected_variants.as_ref()
    {
        let (merged_candidates, merged_inventory, changed_mods) =
            merge_inventory_candidates(previous, &candidates);
        snapshot.candidates = merged_candidates;
        snapshot.inventory = merged_inventory;
        inventory_changed_mods = changed_mods;
    }
    snapshot.generation = generation;
    snapshot.inventory_generation = inventory_generation;
    let raw_projection_changed = !Arc::ptr_eq(&snapshot.raw_files, &raw_files);
    snapshot.raw_files = raw_files;
    if raw_projection_changed {
        let mut capability_flags = BTreeMap::new();
        for raw in snapshot.raw_files.iter() {
            *capability_flags
                .entry(raw.mod_name.to_string())
                .or_insert(0) |= raw.flags;
        }
        snapshot.capability_flags = capability_flags;
    }
    snapshot.loose_beats_archive = intent.loose_beats_archive;
    snapshot.rules_hash = intent.rules_hash.clone();
    snapshot.enabled_mods = Arc::new(
        intent
            .mods
            .iter()
            .filter(|entry| entry.enabled)
            .map(|entry| entry.key.clone())
            .collect(),
    );
    snapshot.selected_variants = selected_variants;
    let ranks = RankContext::new(intent, &snapshot.inventory);
    let mut mods = changed_mods(previous_intent, intent);
    mods.extend(inventory_changed_mods);
    let mut dirty_effective: HashSet<EffectiveKey> = HashSet::new();
    let mut dirty_identities: HashSet<Arc<[u8]>> = HashSet::new();
    for mod_key in &mods {
        if let Some(indexes) = snapshot.inventory.mod_candidates.get(mod_key.as_str()) {
            for index in indexes {
                let candidate = &snapshot.candidates[*index as usize];
                dirty_effective.insert(snapshot.inventory.candidate_effective[*index as usize]);
                dirty_identities.extend(candidate.identities.iter().cloned());
            }
        }
    }
    for archive in archive_rank_changes(previous_intent, intent, &candidates) {
        if let Some(indexes) = snapshot.inventory.archive_candidates.get(&archive) {
            for index in indexes {
                dirty_effective.insert(snapshot.inventory.candidate_effective[*index as usize]);
            }
        }
    }
    if previous_intent.loose_beats_archive != intent.loose_beats_archive {
        dirty_effective.extend(0..snapshot.inventory.effective_postings.len() as u32);
    }
    let candidates_touched = dirty_effective
        .iter()
        .filter_map(|key| snapshot.inventory.effective_postings.get(*key as usize))
        .map(|postings| postings.len() as u64)
        .sum();
    let dirty_elapsed = reconcile_started.elapsed();

    let mut affected: HashSet<i64> = mods
        .iter()
        .filter_map(|key| snapshot.inventory.mod_ids_by_key.get(key.as_str()).copied())
        .collect();
    let mut counter_deltas = HashMap::new();
    let mut edge_deltas = HashMap::new();
    for identity in dirty_identities {
        let state = build_identity_state(&identity, &snapshot, &ranks);
        let toggled = install_identity_state(
            &mut snapshot,
            identity,
            state,
            &mut affected,
            &mut counter_deltas,
            &mut edge_deltas,
        );
        for id in toggled {
            if let Some(candidate) = snapshot.candidate(id) {
                if let Some(index) = snapshot.inventory.candidate_indexes.get(&candidate.id) {
                    dirty_effective.insert(snapshot.inventory.candidate_effective[*index as usize]);
                }
            }
        }
    }
    let identities_elapsed = reconcile_started.elapsed();

    let mut changed_winner_keys = HashSet::new();
    let mut changed_deployed_indexes = HashSet::new();
    let mut touched_winner_ids = Vec::with_capacity(dirty_effective.len());
    let mut destination_build_time = std::time::Duration::ZERO;
    let mut destination_install_time = std::time::Duration::ZERO;
    for effective in dirty_effective.iter().cloned() {
        let destination_started = trace.then(Instant::now);
        let state = build_destination_state(&effective, &snapshot, intent, &ranks);
        let state_built = trace.then(Instant::now);
        install_state(
            &mut snapshot,
            effective,
            state,
            &mut affected,
            &mut counter_deltas,
            &mut edge_deltas,
            &mut changed_winner_keys,
            &mut changed_deployed_indexes,
            &mut touched_winner_ids,
        );
        if let (Some(started), Some(built)) = (destination_started, state_built) {
            destination_build_time += built - started;
            destination_install_time += built.elapsed();
        }
    }
    let destinations_elapsed = reconcile_started.elapsed();
    apply_counter_deltas(&mut snapshot.counters, counter_deltas);
    let changed_edges = apply_edge_deltas(&mut snapshot.edges, edge_deltas);
    let changed_summary_names = finish_summaries(&mut snapshot, intent, &affected);
    let summaries_elapsed = reconcile_started.elapsed();

    // install_state records a path only when its published candidate actually
    // changed, so this set is already the provider delta.  Re-hashing and
    // re-querying all 10k keys here used to consume a large fraction of a hot
    // toggle just to rediscover the same fact.
    refresh_plugin_owners(&mut snapshot, previous, &changed_winner_keys);
    refresh_deployed_indexes(&mut snapshot, previous, &changed_deployed_indexes);
    let casing_changed_keys = refresh_casing(&mut snapshot, previous, &changed_winner_keys);
    let indexes_elapsed = reconcile_started.elapsed();
    let mut actual_winner_keys = changed_winner_keys;
    actual_winner_keys.extend(casing_changed_keys.iter().cloned());
    let mut changed_winner_ids = Vec::new();
    let mut removed_winner_ids = Vec::new();
    for key in &actual_winner_keys {
        match (previous.winners.get(key), snapshot.winners.get(key)) {
            (Some(old), Some(new)) if old == new => {
                if casing_changed_keys.contains(key) {
                    changed_winner_ids.push(*new);
                }
            }
            (Some(old), Some(new)) => {
                removed_winner_ids.push(*old);
                changed_winner_ids.push(*new);
            }
            (None, Some(new)) => changed_winner_ids.push(*new),
            (Some(old), None) => removed_winner_ids.push(*old),
            (None, None) => {}
        }
    }
    touched_winner_ids.extend(changed_winner_ids.iter().copied());
    touched_winner_ids.sort_unstable();
    touched_winner_ids.dedup();
    changed_winner_ids.sort_unstable();
    changed_winner_ids.dedup();
    removed_winner_ids.sort_unstable();
    removed_winner_ids.dedup();
    let changed_plugin_owners = previous
        .plugin_owners
        .keys()
        .chain(snapshot.plugin_owners.keys())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter_map(|name| {
            let old = previous.plugin_owners.get(name);
            let new = snapshot.plugin_owners.get(name);
            (old != new).then(|| (name.clone(), new.cloned()))
        })
        .collect();
    let changed_summaries = changed_summary_names
        .iter()
        .map(|name| {
            (
                name.clone(),
                snapshot.summaries.get(name).cloned().unwrap_or_default(),
            )
        })
        .collect();
    let mut changed_edge_records: Vec<_> = changed_edges
        .iter()
        .filter_map(|edge| {
            snapshot.edge_record(edge, snapshot.edges.get(edge).copied().unwrap_or(0))
        })
        .collect();
    changed_edge_records.sort_by(|left, right| {
        (&left.kind, &left.loser, &left.winner).cmp(&(&right.kind, &right.loser, &right.winner))
    });
    let delta = ResolutionDelta {
        base_generation: previous.generation,
        generation,
        inventory_generation,
        full_rebuild: false,
        candidates_touched,
        destinations_touched: dirty_effective.len() as u64,
        graph_compute_ns: 0,
        sqlite_commit_ns: 0,
        deployment_dirty: !actual_winner_keys.is_empty(),
        changed_winner_ids,
        removed_winner_ids,
        touched_winner_ids,
        changed_summaries,
        changed_plugin_owners,
        changed_edges: changed_edge_records,
    };
    if trace {
        let millis = |later: std::time::Duration, earlier: std::time::Duration| {
            (later - earlier).as_secs_f64() * 1_000.0
        };
        let done = reconcile_started.elapsed();
        eprintln!(
            "[filegraph] incremental mods={} candidates={} destinations={} \
             winners={} deployed_indexes={} casing={} \
             destination_build_ms={:.3} destination_install_ms={:.3} \
             dirty_ms={:.3} identities_ms={:.3} destinations_ms={:.3} \
             summaries_ms={:.3} indexes_ms={:.3} delta_ms={:.3}",
            mods.len(),
            candidates_touched,
            dirty_effective.len(),
            actual_winner_keys.len(),
            changed_deployed_indexes.len(),
            casing_changed_keys.len(),
            destination_build_time.as_secs_f64() * 1_000.0,
            destination_install_time.as_secs_f64() * 1_000.0,
            dirty_elapsed.as_secs_f64() * 1_000.0,
            millis(identities_elapsed, dirty_elapsed),
            millis(destinations_elapsed, identities_elapsed),
            millis(summaries_elapsed, destinations_elapsed),
            millis(indexes_elapsed, summaries_elapsed),
            millis(done, indexes_elapsed),
        );
    }
    GraphUpdate {
        snapshot,
        delta,
        changed_winner_keys: actual_winner_keys,
        changed_edge_keys: changed_edges,
        changed_summary_names,
    }
}

fn update_from_full(previous: &GraphSnapshot, snapshot: GraphSnapshot) -> GraphUpdate {
    let changed_winner_keys: HashSet<_> = previous
        .winners
        .keys()
        .chain(snapshot.winners.keys())
        .filter(
            |key| match (previous.winners.get(*key), snapshot.winners.get(*key)) {
                (Some(old), Some(new)) if old == new => {
                    match (previous.candidate(*old), snapshot.candidate(*new)) {
                        (Some(old_candidate), Some(new_candidate)) => {
                            previous.winner_record(old_candidate, key.namespace)
                                != snapshot.winner_record(new_candidate, key.namespace)
                        }
                        _ => true,
                    }
                }
                (left, right) => left != right,
            },
        )
        .cloned()
        .collect();
    let changed_edge_keys = previous
        .edges
        .keys()
        .chain(snapshot.edges.keys())
        .filter(|key| previous.edges.get(*key) != snapshot.edges.get(*key))
        .cloned()
        .collect();
    let changed_summary_names = previous
        .summaries
        .keys()
        .chain(snapshot.summaries.keys())
        .filter(|name| previous.summaries.get(*name) != snapshot.summaries.get(*name))
        .cloned()
        .collect();
    let mut delta = diff_snapshots(previous, &snapshot);
    delta.full_rebuild = true;
    GraphUpdate {
        snapshot,
        delta,
        changed_winner_keys,
        changed_edge_keys,
        changed_summary_names,
    }
}

pub fn diff_snapshots(previous: &GraphSnapshot, current: &GraphSnapshot) -> ResolutionDelta {
    let mut changed_winner_ids = Vec::new();
    let mut removed_winner_ids = Vec::new();
    for key in previous.winners.keys().chain(current.winners.keys()) {
        match (previous.winners.get(key), current.winners.get(key)) {
            (Some(old), Some(new)) if old == new => {
                if let (Some(old_candidate), Some(new_candidate)) =
                    (previous.candidate(*old), current.candidate(*new))
                    && previous.winner_record(old_candidate, key.namespace)
                        != current.winner_record(new_candidate, key.namespace)
                {
                    changed_winner_ids.push(*new);
                }
            }
            (Some(old), Some(new)) => {
                removed_winner_ids.push(*old);
                changed_winner_ids.push(*new);
            }
            (None, Some(new)) => changed_winner_ids.push(*new),
            (Some(old), None) => removed_winner_ids.push(*old),
            _ => {}
        }
    }
    changed_winner_ids.sort_unstable();
    changed_winner_ids.dedup();
    removed_winner_ids.sort_unstable();
    removed_winner_ids.dedup();
    let mut touched_winner_ids: Vec<_> = current.winners.values().copied().collect();
    touched_winner_ids.sort_unstable();
    touched_winner_ids.dedup();
    let changed_summaries = current
        .summaries
        .iter()
        .filter(|(name, value)| previous.summaries.get(*name) != Some(*value))
        .map(|(name, value)| (name.clone(), value.clone()))
        .chain(
            previous
                .summaries
                .keys()
                .filter(|name| !current.summaries.contains_key(*name))
                .map(|name| (name.clone(), ConflictSummary::default())),
        )
        .collect();
    let changed_plugin_owners = previous
        .plugin_owners
        .keys()
        .chain(current.plugin_owners.keys())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter_map(|name| {
            let old = previous.plugin_owners.get(name);
            let new = current.plugin_owners.get(name);
            (old != new).then(|| (name.clone(), new.cloned()))
        })
        .collect();
    let changed_edge_keys: HashSet<_> = previous
        .edges
        .keys()
        .chain(current.edges.keys())
        .filter(|key| previous.edges.get(*key) != current.edges.get(*key))
        .cloned()
        .collect();
    let mut changed_edges: Vec<_> = changed_edge_keys
        .into_iter()
        .filter_map(|edge| {
            let refcount = current.edges.get(&edge).copied().unwrap_or(0);
            current
                .edge_record(&edge, refcount)
                .or_else(|| previous.edge_record(&edge, refcount))
        })
        .collect();
    changed_edges.sort_by(|left, right| {
        (&left.kind, &left.loser, &left.winner).cmp(&(&right.kind, &right.loser, &right.winner))
    });
    ResolutionDelta {
        base_generation: previous.generation,
        generation: current.generation,
        inventory_generation: current.inventory_generation,
        full_rebuild: true,
        candidates_touched: current.candidates.len() as u64,
        destinations_touched: current.destination_states.len() as u64,
        graph_compute_ns: 0,
        sqlite_commit_ns: 0,
        deployment_dirty: !changed_winner_ids.is_empty() || !removed_winner_ids.is_empty(),
        changed_winner_ids,
        removed_winner_ids,
        touched_winner_ids,
        changed_summaries,
        changed_plugin_owners,
        changed_edges,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{IntentMod, OperationHint};

    #[test]
    fn compact_graph_records_stay_bounded() {
        assert!(std::mem::size_of::<Candidate>() <= 272);
        assert!(std::mem::size_of::<RawCatalogFile>() <= 112);
        assert!(std::mem::size_of::<DestinationState>() <= 48);
        assert!(std::mem::size_of::<EdgeKey>() <= 24);
    }

    fn candidate(id: i64, owner: &str, path: &str) -> Candidate {
        Candidate {
            id,
            destination_id: id,
            mod_id: match owner {
                "A" => 1,
                "B" => 2,
                "C" => 3,
                "[Overwrite]" => 4,
                "[Root_Folder]" => 5,
                _ => 100,
            },
            mod_name: Arc::from(owner),
            mod_key: Arc::from(owner.to_lowercase()),
            variant_key: Arc::from("default"),
            source_rel: Arc::from(path.as_bytes()),
            source_display: Arc::from(path),
            target: Arc::from("game"),
            destination_key: Arc::from(path.as_bytes()),
            destination_display: Arc::from(path),
            kind: ProviderKind::Loose,
            size: 1,
            mtime_ns: 1,
            ordinal: 0,
            identities: Vec::new(),
            archive_key: None,
            plugin_key: None,
            deployable: true,
            legacy_root: false,
            legacy_rel: Arc::from(path),
            flags: 0,
        }
    }

    fn raw_file(id: i64, owner: &str, source: &str, display: &str, flags: u32) -> RawCatalogFile {
        RawCatalogFile {
            id,
            mod_name: Arc::from(owner),
            mod_key: Arc::from(owner.to_lowercase()),
            source_rel: Arc::from(source.as_bytes()),
            source_display: Arc::from(source),
            index_display: Arc::from(display),
            size: 1,
            mtime_ns: 1,
            ordinal: id as u32,
            flags,
        }
    }

    fn displayed_candidate(id: i64, owner: &str, key: &str, display: &str) -> Candidate {
        let mut value = candidate(id, owner, key);
        value.destination_display = Arc::from(display);
        value.legacy_rel = Arc::from(
            display
                .split_once('/')
                .map(|(_, relative)| relative)
                .unwrap_or(display),
        );
        value
    }

    fn archive_candidate(
        id: i64,
        owner: &str,
        archive: &str,
        plugin: &str,
        path: &str,
    ) -> Candidate {
        let mut value = candidate(id, owner, path);
        value.kind = ProviderKind::ArchiveMember;
        value.archive_key = Some(Arc::from(archive));
        value.plugin_key = Some(Arc::from(plugin));
        value.deployable = false;
        value
    }

    fn root_candidate(id: i64, owner: &str, path: &str) -> Candidate {
        let mut value = candidate(id, owner, path);
        value.kind = ProviderKind::Root;
        value.legacy_root = true;
        value
    }

    fn intent() -> ProfileIntent {
        ProfileIntent {
            profile_id: "test".to_owned(),
            intent_hash: vec![1],
            rules_hash: vec![1],
            mods: vec![
                IntentMod {
                    name: "C".into(),
                    key: "c".into(),
                    enabled: true,
                    variant_key: "default".into(),
                },
                IntentMod {
                    name: "B".into(),
                    key: "b".into(),
                    enabled: true,
                    variant_key: "default".into(),
                },
                IntentMod {
                    name: "A".into(),
                    key: "a".into(),
                    enabled: true,
                    variant_key: "default".into(),
                },
            ],
            special_variants: BTreeMap::new(),
            archive_order: Vec::new(),
            plugin_order: Vec::new(),
            plugin_extensions: Vec::new(),
            disabled_plugin_paths: BTreeMap::new(),
            loose_beats_archive: true,
            normalize_folder_case: false,
            casing_strategy: "upper".to_owned(),
            casing_pins: BTreeMap::new(),
            hint: OperationHint::default(),
        }
    }

    #[test]
    fn records_direct_neighbours_only() {
        let snapshot = build_full(
            Arc::new(vec![
                candidate(1, "A", "same"),
                candidate(2, "B", "same"),
                candidate(3, "C", "same"),
            ]),
            Arc::new(Vec::new()),
            &intent(),
            1,
            1,
        );
        let export = snapshot.export();
        assert!(
            export
                .edges
                .iter()
                .any(|edge| edge.loser == "A" && edge.winner == "B")
        );
        assert!(
            export
                .edges
                .iter()
                .any(|edge| edge.loser == "B" && edge.winner == "C")
        );
        assert!(
            !export
                .edges
                .iter()
                .any(|edge| edge.loser == "A" && edge.winner == "C")
        );
        assert_eq!(export.winners[0].mod_name, "C");
    }

    #[test]
    fn compact_mod_plugin_query_matches_full_mod_file_projection() {
        let mut plugin = candidate(1, "A", "plugin.esp");
        plugin.destination_display = Arc::from("Data/Plugin.ESP");
        plugin.destination_key = Arc::from(&b"data/plugin.esp"[..]);
        plugin.legacy_rel = Arc::from("Plugin.ESP");
        plugin.plugin_key = Some(Arc::from("plugin.esp"));
        plugin.flags = 1 << 7;
        let snapshot = build_full(
            Arc::new(vec![plugin]),
            Arc::new(vec![
                // A flagged raw plugin without a deploy candidate retains the
                // same fallback spelling exposed by mod_files().
                raw_file(2, "A", "excluded.esl", "Excluded.ESL", 1 << 7),
                raw_file(1, "A", "plugin.esp", "plugin.esp", 1 << 7),
                raw_file(3, "A", "textures/a.dds", "textures/a.dds", 0),
            ]),
            &intent(),
            1,
            1,
        );
        let projected: Vec<_> = snapshot
            .mod_files("A")
            .into_iter()
            .filter(|record| record.plugin_key.is_some())
            .map(|record| {
                record
                    .destination_display
                    .replace('\\', "/")
                    .rsplit('/')
                    .next()
                    .unwrap_or(&record.destination_display)
                    .to_owned()
            })
            .collect();

        assert_eq!(snapshot.mod_plugins("A"), projected);
        assert_eq!(projected, vec!["Excluded.ESL", "Plugin.ESP"]);
    }

    #[test]
    fn incremental_toggle_touches_only_provider_destinations() {
        let candidates = Arc::new(vec![
            candidate(1, "A", "same"),
            candidate(2, "A", "only-a"),
            candidate(3, "B", "same"),
            candidate(4, "B", "only-b"),
        ]);
        let first_intent = intent();
        let first = build_full(
            candidates.clone(),
            Arc::new(Vec::new()),
            &first_intent,
            1,
            1,
        );
        let mut next_intent = first_intent.clone();
        next_intent.mods[1].enabled = false;
        next_intent.hint = OperationHint {
            kind: "toggle".to_owned(),
            mods: vec!["B".to_owned()],
        };
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            candidates,
            Arc::new(Vec::new()),
            &next_intent,
            1,
            2,
        );
        assert_eq!(update.delta.destinations_touched, 2);
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Normal, "game", b"same")
                .unwrap()
                .mod_name,
            "A"
        );
    }

    #[test]
    fn priority_move_does_not_force_a_full_rebuild() {
        let candidates = Arc::new(vec![
            candidate(1, "A", "same"),
            candidate(2, "A", "only-a"),
            candidate(3, "B", "same"),
            candidate(4, "B", "only-b"),
            candidate(5, "C", "only-c"),
        ]);
        let first_intent = intent();
        let first = build_full(
            candidates.clone(),
            Arc::new(Vec::new()),
            &first_intent,
            1,
            1,
        );
        let mut moved_intent = first_intent.clone();
        moved_intent.mods.swap(1, 2);
        moved_intent.hint = OperationHint {
            kind: "move".to_owned(),
            mods: vec!["A".to_owned()],
        };
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            candidates,
            Arc::new(Vec::new()),
            &moved_intent,
            1,
            2,
        );
        assert!(!update.delta.full_rebuild);
        assert_eq!(update.delta.destinations_touched, 2);
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Normal, "game", b"same")
                .unwrap()
                .mod_name,
            "A"
        );
    }

    #[test]
    fn inventory_add_touches_only_the_new_mod_destinations() {
        let mut first_intent = intent();
        first_intent.mods.retain(|entry| entry.key != "c");
        let first_candidates = Arc::new(vec![
            candidate(1, "A", "shared"),
            candidate(2, "A", "only-a"),
            candidate(3, "B", "shared"),
            candidate(4, "B", "only-b"),
        ]);
        let first = build_full(first_candidates, Arc::new(Vec::new()), &first_intent, 1, 1);

        let mut added_intent = first_intent.clone();
        added_intent.mods.insert(
            0,
            IntentMod {
                name: "C".into(),
                key: "c".into(),
                enabled: true,
                variant_key: "default".into(),
            },
        );
        added_intent.hint = OperationHint {
            kind: "install".to_owned(),
            mods: vec!["C".to_owned()],
        };
        let current_candidates = Arc::new(vec![
            candidate(1, "A", "shared"),
            candidate(2, "A", "only-a"),
            candidate(3, "B", "shared"),
            candidate(4, "B", "only-b"),
            candidate(5, "C", "shared"),
            candidate(6, "C", "only-c"),
        ]);
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            current_candidates.clone(),
            Arc::new(Vec::new()),
            &added_intent,
            2,
            2,
        );
        let rebuilt = build_full(
            current_candidates,
            Arc::new(Vec::new()),
            &added_intent,
            2,
            2,
        );
        assert!(!update.delta.full_rebuild);
        assert_eq!(update.delta.destinations_touched, 2);
        assert_eq!(update.snapshot.export().winners, rebuilt.export().winners);
        assert_eq!(update.snapshot.export().edges, rebuilt.export().edges);
        assert_eq!(
            update.snapshot.export().summaries,
            rebuilt.export().summaries
        );
    }

    #[test]
    fn toggle_after_inventory_add_reuses_the_merged_inventory() {
        let mut first_intent = intent();
        first_intent.mods.retain(|entry| entry.key != "c");
        let first = build_full(
            Arc::new(vec![
                candidate(1, "A", "shared"),
                candidate(2, "B", "shared"),
            ]),
            Arc::new(Vec::new()),
            &first_intent,
            1,
            1,
        );

        let mut added_intent = first_intent.clone();
        added_intent.mods.insert(
            0,
            IntentMod {
                name: "C".into(),
                key: "c".into(),
                enabled: true,
                variant_key: "default".into(),
            },
        );
        added_intent.hint = OperationHint {
            kind: "install".to_owned(),
            mods: vec!["C".to_owned()],
        };
        // This is the compact allocation retained by the catalog cache.  The
        // incremental graph creates a different allocation with stable slots.
        let catalog_candidates = Arc::new(vec![
            candidate(1, "A", "shared"),
            candidate(2, "B", "shared"),
            candidate(3, "C", "only-c"),
        ]);
        let added = reconcile_graph(
            &first,
            Some(&first_intent),
            catalog_candidates.clone(),
            Arc::new(Vec::new()),
            &added_intent,
            2,
            2,
        );
        assert!(!Arc::ptr_eq(
            &added.snapshot.candidates,
            &catalog_candidates
        ));

        let mut toggled_intent = added_intent.clone();
        toggled_intent.mods[0].enabled = false;
        toggled_intent.hint = OperationHint {
            kind: "toggle".to_owned(),
            mods: vec!["C".to_owned()],
        };
        let previous_inventory = added.snapshot.inventory.clone();
        let toggled = reconcile_graph(
            &added.snapshot,
            Some(&added_intent),
            catalog_candidates,
            Arc::new(Vec::new()),
            &toggled_intent,
            2,
            3,
        );

        assert!(Arc::ptr_eq(
            &previous_inventory,
            &toggled.snapshot.inventory
        ));
        assert_eq!(toggled.delta.candidates_touched, 1);
        assert_eq!(toggled.delta.destinations_touched, 1);
    }

    #[test]
    fn inventory_remove_reveals_previous_winner_incrementally() {
        let mut first_intent = intent();
        first_intent
            .mods
            .retain(|entry| matches!(entry.key.as_str(), "a" | "b"));
        let first_candidates = Arc::new(vec![
            candidate(1, "A", "shared"),
            candidate(2, "A", "only-a"),
            candidate(3, "B", "shared"),
            candidate(4, "B", "only-b"),
        ]);
        let first = build_full(first_candidates, Arc::new(Vec::new()), &first_intent, 1, 1);
        assert_eq!(
            first
                .winner(Namespace::Normal, "game", b"shared")
                .unwrap()
                .mod_name,
            "B"
        );

        let mut removed_intent = first_intent.clone();
        removed_intent.mods.retain(|entry| entry.key != "b");
        removed_intent.hint = OperationHint {
            kind: "remove".to_owned(),
            mods: vec!["B".to_owned()],
        };
        let current_candidates = Arc::new(vec![
            candidate(1, "A", "shared"),
            candidate(2, "A", "only-a"),
        ]);
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            current_candidates.clone(),
            Arc::new(Vec::new()),
            &removed_intent,
            2,
            2,
        );
        let rebuilt = build_full(
            current_candidates,
            Arc::new(Vec::new()),
            &removed_intent,
            2,
            2,
        );
        assert!(!update.delta.full_rebuild);
        assert_eq!(update.delta.destinations_touched, 2);
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Normal, "game", b"shared")
                .unwrap()
                .mod_name,
            "A"
        );
        assert_eq!(update.snapshot.export().winners, rebuilt.export().winners);
        assert_eq!(update.snapshot.export().edges, rebuilt.export().edges);
        assert_eq!(
            update.snapshot.export().summaries,
            rebuilt.export().summaries
        );
    }

    #[test]
    fn inventory_reinstall_replaces_old_provider_ids_incrementally() {
        let mut profile = intent();
        profile
            .mods
            .retain(|entry| matches!(entry.key.as_str(), "a" | "b"));
        let first = build_full(
            Arc::new(vec![
                candidate(1, "A", "shared"),
                candidate(2, "A", "only-a"),
                candidate(3, "B", "shared"),
                candidate(4, "B", "removed-by-update"),
            ]),
            Arc::new(Vec::new()),
            &profile,
            1,
            1,
        );
        let current_candidates = Arc::new(vec![
            candidate(1, "A", "shared"),
            candidate(2, "A", "only-a"),
            candidate(30, "B", "shared"),
            candidate(31, "B", "added-by-update"),
        ]);
        let mut reinstalled = profile.clone();
        reinstalled.hint = OperationHint {
            kind: "reinstall".to_owned(),
            mods: vec!["B".to_owned()],
        };
        let update = reconcile_graph(
            &first,
            Some(&profile),
            current_candidates.clone(),
            Arc::new(Vec::new()),
            &reinstalled,
            2,
            2,
        );
        let rebuilt = build_full(current_candidates, Arc::new(Vec::new()), &reinstalled, 2, 2);
        assert!(!update.delta.full_rebuild);
        assert_eq!(update.delta.destinations_touched, 3);
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Normal, "game", b"shared")
                .unwrap()
                .candidate_id,
            30
        );
        assert_eq!(update.snapshot.export().winners, rebuilt.export().winners);
        assert_eq!(update.snapshot.export().edges, rebuilt.export().edges);
        assert_eq!(
            update.snapshot.export().summaries,
            rebuilt.export().summaries
        );
    }

    #[test]
    #[ignore = "release-only synthetic performance check"]
    fn benchmark_inventory_add_one_hundred_thousand_candidates() {
        let count = 100_000_i64;
        let first_candidates: Vec<_> = (0..count)
            .map(|index| candidate(index + 1, "A", &format!("textures/{index:06}.dds")))
            .collect();
        let mut first_intent = intent();
        first_intent.mods.retain(|entry| entry.key == "a");
        let first = build_full(
            Arc::new(first_candidates.clone()),
            Arc::new(Vec::new()),
            &first_intent,
            1,
            1,
        );
        let mut current_candidates = first_candidates;
        current_candidates.extend(
            (0..count).map(|index| {
                candidate(count + index + 1, "B", &format!("textures/{index:06}.dds"))
            }),
        );
        let mut added_intent = first_intent.clone();
        added_intent.mods.insert(
            0,
            IntentMod {
                name: "B".into(),
                key: "b".into(),
                enabled: true,
                variant_key: "default".into(),
            },
        );
        added_intent.hint = OperationHint {
            kind: "install".to_owned(),
            mods: vec!["B".to_owned()],
        };
        let started = Instant::now();
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            Arc::new(current_candidates),
            Arc::new(Vec::new()),
            &added_intent,
            2,
            2,
        );
        let elapsed = started.elapsed();
        eprintln!(
            "[FILEGRAPH-BENCH] inventory add 100000: {:.3}s, touched={} destinations={}",
            elapsed.as_secs_f64(),
            update.delta.candidates_touched,
            update.delta.destinations_touched,
        );
        assert!(!update.delta.full_rebuild);
        assert_eq!(update.delta.destinations_touched, count as u64);
        assert_eq!(update.snapshot.export().winners.len(), count as usize);
    }

    #[test]
    fn priority_move_dirties_only_moved_mod_archive_paths() {
        let mut a = archive_candidate(1, "A", "a.bsa", "", "data/shared.dds");
        let mut b = archive_candidate(2, "B", "b.bsa", "", "data/shared.dds");
        let mut c = archive_candidate(3, "C", "c.bsa", "", "data/only-c.dds");
        a.plugin_key = None;
        b.plugin_key = None;
        c.plugin_key = None;
        let candidates = Arc::new(vec![a, b, c]);
        let mut first_intent = intent();
        first_intent.archive_order = vec!["a.bsa".into(), "b.bsa".into(), "c.bsa".into()];
        let first = build_full(
            candidates.clone(),
            Arc::new(Vec::new()),
            &first_intent,
            1,
            1,
        );
        assert_eq!(
            first
                .winner(Namespace::Archive, "game", b"data/shared.dds")
                .unwrap()
                .mod_name,
            "B"
        );

        let mut moved_intent = first_intent.clone();
        moved_intent.mods.swap(1, 2);
        moved_intent.archive_order = vec!["b.bsa".into(), "a.bsa".into(), "c.bsa".into()];
        moved_intent.hint = OperationHint {
            kind: "move".to_owned(),
            mods: vec!["A".to_owned()],
        };
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            candidates,
            Arc::new(Vec::new()),
            &moved_intent,
            1,
            2,
        );
        assert!(!update.delta.full_rebuild);
        assert_eq!(update.delta.destinations_touched, 1);
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Archive, "game", b"data/shared.dds")
                .unwrap()
                .mod_name,
            "A"
        );
    }

    #[test]
    fn disabled_winning_plugin_reveals_the_next_provider() {
        let mut profile = intent();
        profile
            .disabled_plugin_paths
            .insert("c".to_owned(), [b"same".to_vec()].into_iter().collect());
        let snapshot = build_full(
            Arc::new(vec![candidate(1, "A", "same"), candidate(3, "C", "same")]),
            Arc::new(Vec::new()),
            &profile,
            1,
            1,
        );
        assert_eq!(
            snapshot
                .winner(Namespace::Normal, "game", b"same")
                .unwrap()
                .mod_name,
            "A"
        );
    }

    #[test]
    fn winner_casing_changes_invalidate_affected_subtrees() {
        let mut profile = intent();
        profile.normalize_folder_case = true;
        let candidates = Arc::new(vec![
            displayed_candidate(2, "B", "data/meshes/a.txt", "data/meshes/a.txt"),
            displayed_candidate(3, "C", "data/meshes/other.txt", "Data/MESHES/other.txt"),
        ]);
        let snapshot = build_full(candidates.clone(), Arc::new(Vec::new()), &profile, 1, 1);
        assert_eq!(
            snapshot
                .winner(Namespace::Normal, "game", b"data/meshes/a.txt")
                .unwrap()
                .destination_display,
            "Data/MESHES/a.txt"
        );

        let mut changed = profile.clone();
        changed.mods[0].enabled = false;
        changed.hint = OperationHint {
            kind: "toggle".to_owned(),
            mods: vec!["C".to_owned()],
        };
        let update = reconcile_graph(
            &snapshot,
            Some(&profile),
            candidates,
            Arc::new(Vec::new()),
            &changed,
            1,
            2,
        );
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Normal, "game", b"data/meshes/a.txt")
                .unwrap()
                .destination_display,
            "data/meshes/a.txt"
        );
        assert!(update.delta.changed_winner_ids.contains(&2));
        assert!(update.delta.deployment_dirty);
    }

    #[test]
    fn batched_casing_update_matches_full_rebuild() {
        let mut profile = intent();
        profile.normalize_folder_case = true;
        let mut values = Vec::new();
        for index in 0..600_i64 {
            let key = format!("data/meshes/generated/{index:04}.txt");
            values.push(displayed_candidate(
                index * 2 + 1,
                "A",
                &key,
                &format!("data/meshes/generated/{index:04}.txt"),
            ));
            values.push(displayed_candidate(
                index * 2 + 2,
                "B",
                &key,
                &format!("Data/MESHES/Generated/{index:04}.txt"),
            ));
        }
        let candidates = Arc::new(values);
        let first = build_full(candidates.clone(), Arc::new(Vec::new()), &profile, 1, 1);
        let mut changed = profile.clone();
        changed.mods[1].enabled = false;
        changed.hint = OperationHint {
            kind: "toggle".to_owned(),
            mods: vec!["B".to_owned()],
        };
        let update = reconcile_graph(
            &first,
            Some(&profile),
            candidates.clone(),
            Arc::new(Vec::new()),
            &changed,
            1,
            2,
        );
        let rebuilt = build_full(candidates, Arc::new(Vec::new()), &changed, 1, 2);
        let incremental_export = update.snapshot.export();
        let rebuilt_export = rebuilt.export();
        assert_eq!(incremental_export.winners, rebuilt_export.winners);
        assert_eq!(incremental_export.summaries, rebuilt_export.summaries);
        assert_eq!(incremental_export.edges, rebuilt_export.edges);
        assert_eq!(
            update.snapshot.deployment_plan().entries,
            rebuilt.deployment_plan().entries
        );
    }

    #[test]
    fn casing_pins_apply_even_when_normalization_is_disabled() {
        let mut profile = intent();
        profile
            .casing_pins
            .insert("meshes".to_owned(), "Meshes".to_owned());
        profile
            .casing_pins
            .insert("a.txt".to_owned(), "A.TXT".to_owned());
        let snapshot = build_full(
            Arc::new(vec![displayed_candidate(
                2,
                "B",
                "data/meshes/a.txt",
                "data/meshes/a.txt",
            )]),
            Arc::new(Vec::new()),
            &profile,
            1,
            1,
        );
        assert_eq!(
            snapshot.deployment_plan().entries[0].destination_display,
            "data/Meshes/A.TXT"
        );
    }

    #[test]
    fn plugin_reorder_reranks_only_affected_archive_paths() {
        let candidates = Arc::new(vec![
            archive_candidate(1, "A", "a\0alpha.bsa", "alpha", "data/shared.dds"),
            archive_candidate(2, "B", "b\0beta.bsa", "beta", "data/shared.dds"),
        ]);
        let mut first_intent = intent();
        first_intent.archive_order = vec!["a\0alpha.bsa".into(), "b\0beta.bsa".into()];
        first_intent.plugin_order = vec!["alpha".into(), "beta".into()];
        let first = build_full(
            candidates.clone(),
            Arc::new(Vec::new()),
            &first_intent,
            1,
            1,
        );
        assert_eq!(
            first
                .winner(Namespace::Archive, "game", b"data/shared.dds")
                .unwrap()
                .mod_name,
            "B"
        );

        let mut reordered = first_intent.clone();
        reordered.plugin_order = vec!["beta".into(), "alpha".into()];
        reordered.hint.kind = "plugin_order".into();
        let update = reconcile_graph(
            &first,
            Some(&first_intent),
            candidates,
            Arc::new(Vec::new()),
            &reordered,
            1,
            2,
        );
        assert_eq!(update.delta.destinations_touched, 1);
        assert!(!update.delta.full_rebuild);
        assert_eq!(
            update
                .snapshot
                .winner(Namespace::Archive, "game", b"data/shared.dds")
                .unwrap()
                .mod_name,
            "A"
        );
    }

    #[test]
    fn root_phase_wins_and_records_cross_namespace_conflict() {
        let snapshot = build_full(
            Arc::new(vec![
                candidate(1, "A", "data/shared.txt"),
                root_candidate(2, "B", "data/shared.txt"),
            ]),
            Arc::new(Vec::new()),
            &intent(),
            1,
            1,
        );
        let plan = snapshot.deployment_plan();
        assert_eq!(plan.entries.len(), 1);
        assert_eq!(plan.entries[0].mod_name, "B");
        assert_eq!(plan.entries[0].provider_kind, ProviderKind::Root);
        assert!(snapshot.edges.contains_key(&EdgeKey {
            kind: ConflictKind::Loose,
            loser: 1,
            winner: 2,
        }));
        assert_eq!(snapshot.summaries["A"].loose_code, 3);
        assert_eq!(snapshot.summaries["B"].loose_code, 1);
    }
}
