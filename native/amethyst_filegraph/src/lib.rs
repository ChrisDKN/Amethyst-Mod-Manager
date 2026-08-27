mod catalog;
mod error;
#[path = "incremental_graph.rs"]
mod graph;
mod model;
mod schema;

use crate::catalog::{LibraryCore, ProfileCore, database_name};
use crate::error::{FileGraphError, Result};
use crate::graph::GraphSnapshot;
use crate::model::{
    API_VERSION, ManifestBatch, Namespace, ProfileIntent, ProviderKind, SCHEMA_VERSION,
};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

fn encode<T: serde::Serialize>(value: &T) -> Result<Vec<u8>> {
    Ok(rmp_serde::to_vec_named(value)?)
}

fn decode<'a, T: serde::Deserialize<'a>>(bytes: &'a [u8]) -> Result<T> {
    Ok(rmp_serde::from_slice(bytes)?)
}

fn encoded_py<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyBytes>> {
    let bytes = encode(value).map_err(PyErr::from)?;
    Ok(PyBytes::new(py, &bytes).unbind())
}

fn encoded_py_compact<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyBytes>> {
    let bytes = rmp_serde::to_vec(value)
        .map_err(FileGraphError::from)
        .map_err(PyErr::from)?;
    Ok(PyBytes::new(py, &bytes).unbind())
}

#[pyclass]
#[derive(Clone)]
struct CancelToken {
    value: Arc<AtomicBool>,
}

#[pymethods]
impl CancelToken {
    #[new]
    fn new() -> Self {
        Self {
            value: Arc::new(AtomicBool::new(false)),
        }
    }

    fn cancel(&self) {
        self.value.store(true, Ordering::Release);
    }

    fn reset(&self) {
        self.value.store(false, Ordering::Release);
    }

    fn is_cancelled(&self) -> bool {
        self.value.load(Ordering::Acquire)
    }
}

#[pyclass]
struct LibrarySession {
    core: Arc<LibraryCore>,
}

#[pymethods]
impl LibrarySession {
    #[staticmethod]
    fn open(py: Python<'_>, root: PathBuf) -> PyResult<Self> {
        let path = database_name(&root);
        let core = py
            .detach(move || LibraryCore::open(path))
            .map_err(PyErr::from)?;
        Ok(Self { core })
    }

    #[getter]
    fn database_path(&self) -> PathBuf {
        self.core.database_path.clone()
    }

    fn status(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        let status = py.detach(move || core.status()).map_err(PyErr::from)?;
        encoded_py(py, &status)
    }

    fn mod_names(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        let core = self.core.clone();
        py.detach(move || core.mod_names()).map_err(PyErr::from)
    }

    fn manifest_fingerprints(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        let fingerprints = py
            .detach(move || core.manifest_fingerprints())
            .map_err(PyErr::from)?;
        encoded_py(py, &fingerprints)
    }

    fn manifest_for_rederive(&self, py: Python<'_>, mod_key: String) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        let manifest = py
            .detach(move || core.manifest_for_rederive(&mod_key))
            .map_err(PyErr::from)?;
        encoded_py(py, &manifest)
    }

    fn variant_keys(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        let variants = py
            .detach(move || core.variant_keys())
            .map_err(PyErr::from)?;
        encoded_py(py, &variants)
    }

    fn archive_units(&self, py: Python<'_>, payload: &[u8]) -> PyResult<Py<PyBytes>> {
        let selected: Vec<(String, String)> = decode(payload).map_err(PyErr::from)?;
        let core = self.core.clone();
        let archives = py
            .detach(move || core.archive_units(&selected))
            .map_err(PyErr::from)?;
        encoded_py(py, &archives)
    }

    fn set_ready(&self, py: Python<'_>, ready: bool) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.set_ready(ready))
            .map_err(PyErr::from)
    }

    fn reset_catalog(&self, py: Python<'_>) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.reset_catalog()).map_err(PyErr::from)
    }

    fn checkpoint(&self, py: Python<'_>) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.checkpoint()).map_err(PyErr::from)
    }

    fn activate_catalog(&self, py: Python<'_>, source_database: PathBuf) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.activate_catalog(&source_database))
            .map_err(PyErr::from)
    }

    #[pyo3(signature = (payload, cancel=None))]
    fn replace_mod_manifest(
        &self,
        py: Python<'_>,
        payload: &[u8],
        cancel: Option<PyRef<'_, CancelToken>>,
    ) -> PyResult<u64> {
        let batch: ManifestBatch = decode(payload).map_err(PyErr::from)?;
        let core = self.core.clone();
        let cancelled = cancel
            .map(|token| token.value.clone())
            .unwrap_or_else(|| Arc::new(AtomicBool::new(false)));
        py.detach(move || core.replace_manifest(batch, &cancelled))
            .map_err(PyErr::from)
    }

    fn remove_mod(&self, py: Python<'_>, mod_key: String) -> PyResult<bool> {
        let core = self.core.clone();
        py.detach(move || core.remove_mod(&mod_key))
            .map_err(PyErr::from)
    }

    fn rename_mod(
        &self,
        py: Python<'_>,
        old_key: String,
        new_key: String,
        new_display: String,
    ) -> PyResult<bool> {
        let core = self.core.clone();
        py.detach(move || core.rename_mod(&old_key, &new_key, &new_display))
            .map_err(PyErr::from)
    }

    fn open_profile(&self, py: Python<'_>, profile_id: String) -> PyResult<ProfileSession> {
        let core = self.core.clone();
        let profile = py
            .detach(move || ProfileCore::new(core, profile_id))
            .map_err(PyErr::from)?;
        Ok(ProfileSession { core: profile })
    }
}

#[pyclass]
struct ProfileSession {
    core: Arc<ProfileCore>,
}

#[pymethods]
impl ProfileSession {
    #[getter]
    fn profile_id(&self) -> String {
        self.core.profile_id.clone()
    }

    #[pyo3(signature = (payload, cancel_token=None))]
    fn reconcile(
        &self,
        py: Python<'_>,
        payload: &[u8],
        cancel_token: Option<PyRef<'_, CancelToken>>,
    ) -> PyResult<Py<PyBytes>> {
        let intent: ProfileIntent = decode(payload).map_err(PyErr::from)?;
        let core = self.core.clone();
        let cancel = cancel_token
            .map(|token| token.value.clone())
            .unwrap_or_else(|| Arc::new(AtomicBool::new(false)));
        let mut delta = py
            .detach(move || core.reconcile(intent, &cancel))
            .map_err(PyErr::from)?;
        // In the common winner-changing edit these vectors are identical.
        // An empty touched vector on the wire aliases changed_winner_ids; the
        // Python model expands it, avoiding a second 10k-ID serialization.
        if delta.touched_winner_ids == delta.changed_winner_ids {
            delta.touched_winner_ids.clear();
        }
        encoded_py(py, &delta)
    }

    fn snapshot(&self) -> ResolvedSnapshot {
        ResolvedSnapshot {
            snapshot: self.core.snapshot(),
        }
    }

    fn build_deployment_plan(
        &self,
        py: Python<'_>,
        snapshot_generation: u64,
    ) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        // Sorting the winners and serialising tens of thousands of entries is
        // Deploy-stage work. Release the GIL while constructing the plan and
        // return to Python only to create the immutable bytes object.
        let bytes = py
            .detach(move || {
                let plan = core.deployment_plan(snapshot_generation)?;
                rmp_serde::to_vec(plan.as_ref()).map_err(FileGraphError::from)
            })
            .map_err(PyErr::from)?;
        Ok(PyBytes::new(py, &bytes).unbind())
    }

    fn begin_prepared_deployment(
        &self,
        py: Python<'_>,
        operation_id: String,
        snapshot_generation: u64,
        link_mode: String,
    ) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || {
            core.begin_prepared_deployment(&operation_id, snapshot_generation, &link_mode)
        })
        .map_err(PyErr::from)
    }

    fn begin_deployment(
        &self,
        py: Python<'_>,
        operation_id: String,
        snapshot_generation: u64,
        link_mode: String,
    ) -> PyResult<Py<PyBytes>> {
        let begin_started = Instant::now();
        let core = self.core.clone();
        let plan = py
            .detach(move || core.begin_deployment(&operation_id, snapshot_generation, &link_mode))
            .map_err(PyErr::from)?;
        let native_elapsed = begin_started.elapsed();
        let result = encoded_py_compact(py, &plan)?;
        let total_elapsed = begin_started.elapsed();
        if crate::model::perftrace_enabled() {
            eprintln!(
                "[FILEGRAPH-TIMING] deploy begin: native plan+journal {:.3}s, \
                 wire encode {:.3}s, total {:.3}s ({} entries)",
                native_elapsed.as_secs_f64(),
                (total_elapsed - native_elapsed).as_secs_f64(),
                total_elapsed.as_secs_f64(),
                plan.entries.len(),
            );
        }
        Ok(result)
    }

    fn deployment_unchanged(
        &self,
        py: Python<'_>,
        snapshot_generation: u64,
        link_mode: String,
    ) -> PyResult<bool> {
        let core = self.core.clone();
        py.detach(move || core.deployment_unchanged(snapshot_generation, &link_mode))
            .map_err(PyErr::from)
    }

    fn commit_deployment(&self, py: Python<'_>, operation_id: String) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.commit_deployment(&operation_id))
            .map_err(PyErr::from)
    }

    fn update_deployment_phase(
        &self,
        py: Python<'_>,
        operation_id: String,
        phase: String,
    ) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.update_deployment_phase(&operation_id, &phase))
            .map_err(PyErr::from)
    }

    fn fail_deployment(&self, py: Python<'_>, operation_id: String) -> PyResult<()> {
        let core = self.core.clone();
        py.detach(move || core.fail_deployment(&operation_id))
            .map_err(PyErr::from)
    }

    fn incomplete_operations(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        let operations = py
            .detach(move || core.incomplete_operations())
            .map_err(PyErr::from)?;
        encoded_py(py, &operations)
    }

    fn deployed_entries(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        let core = self.core.clone();
        let entries = py
            .detach(move || core.deployed_entries())
            .map_err(PyErr::from)?;
        encoded_py(py, &entries)
    }

    fn forget_deployed_mods(&self, py: Python<'_>, mod_keys: Vec<String>) -> PyResult<u64> {
        let core = self.core.clone();
        py.detach(move || core.forget_deployed_mods(&mod_keys))
            .map_err(PyErr::from)
    }
}

#[pyclass]
struct ResolvedSnapshot {
    snapshot: Arc<GraphSnapshot>,
}

fn parse_namespace(value: &str) -> PyResult<Namespace> {
    match value {
        "normal" | "loose" => Ok(Namespace::Normal),
        "root" => Ok(Namespace::Root),
        "archive" => Ok(Namespace::Archive),
        _ => Err(FileGraphError::Invalid(format!("unknown namespace {value:?}")).into()),
    }
}

fn parse_provider_kind(value: &str) -> PyResult<ProviderKind> {
    match value {
        "loose" => Ok(ProviderKind::Loose),
        "root" => Ok(ProviderKind::Root),
        "overwrite" => Ok(ProviderKind::Overwrite),
        "archive_member" => Ok(ProviderKind::ArchiveMember),
        _ => Err(FileGraphError::Invalid(format!("unknown provider kind {value:?}")).into()),
    }
}

#[pymethods]
impl ResolvedSnapshot {
    #[getter]
    fn generation(&self) -> u64 {
        self.snapshot.generation
    }

    #[getter]
    fn inventory_generation(&self) -> u64 {
        self.snapshot.inventory_generation
    }

    #[getter]
    fn loose_beats_archive(&self) -> bool {
        self.snapshot.loose_beats_archive()
    }

    fn export(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.export())
    }

    #[pyo3(signature = (target=None, namespaces=Vec::new(), after_id=0, limit=1000))]
    fn iter_winners(
        &self,
        py: Python<'_>,
        target: Option<String>,
        namespaces: Vec<String>,
        after_id: i64,
        limit: usize,
    ) -> PyResult<Py<PyBytes>> {
        let namespaces = namespaces
            .iter()
            .map(|value| parse_namespace(value))
            .collect::<PyResult<BTreeSet<_>>>()?;
        encoded_py(
            py,
            &self.snapshot.iter_winners(
                target.as_deref(),
                &namespaces,
                after_id,
                limit.clamp(1, 10_000),
            ),
        )
    }

    fn conflict_state(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.conflict_state())
    }

    fn framework_winners(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.framework_winners())
    }

    fn flagged_winners(&self, py: Python<'_>, flags: u32) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.flagged_winners(flags))
    }

    fn staged_plugins(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.staged_plugins())
    }

    fn plugin_winners(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.plugin_winners())
    }

    #[pyo3(signature = (path, basename=false))]
    fn has_deployed_path(&self, path: &[u8], basename: bool) -> bool {
        self.snapshot.has_deployed_path(path, basename)
    }

    fn patch_files(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.patch_files())
    }

    fn mod_files(&self, py: Python<'_>, mod_name: &str) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.mod_files(mod_name))
    }

    fn mod_plugins(&self, py: Python<'_>, mod_name: String) -> PyResult<Py<PyBytes>> {
        let snapshot = self.snapshot.clone();
        let plugins = py.detach(move || snapshot.mod_plugins(&mod_name));
        encoded_py(py, &plugins)
    }

    #[pyo3(signature = (mod_name, winners_only=false, kinds=Vec::new(), cursor=0, limit=1000))]
    fn iter_mod_files(
        &self,
        py: Python<'_>,
        mod_name: &str,
        winners_only: bool,
        kinds: Vec<String>,
        cursor: usize,
        limit: usize,
    ) -> PyResult<Py<PyBytes>> {
        let kinds = kinds
            .iter()
            .map(|value| parse_provider_kind(value))
            .collect::<PyResult<BTreeSet<_>>>()?;
        encoded_py(
            py,
            &self.snapshot.iter_mod_files(
                mod_name,
                winners_only,
                &kinds,
                cursor,
                limit.clamp(1, 10_000),
            ),
        )
    }

    fn archive_files(&self, py: Python<'_>, mod_name: &str) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.archive_files(mod_name))
    }

    fn inventory_facets(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.inventory_facets())
    }

    fn raw_files_by_basename(
        &self,
        py: Python<'_>,
        basenames: Vec<String>,
    ) -> PyResult<Py<PyBytes>> {
        let basenames = basenames
            .into_iter()
            .map(|name| name.to_lowercase())
            .collect();
        encoded_py(py, &self.snapshot.raw_files_by_basename(&basenames))
    }

    fn winner_by_suffix(&self, py: Python<'_>, suffix: &[u8]) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.winner_by_suffix(suffix))
    }

    fn asset_winners(&self, py: Python<'_>, prefixes: Vec<String>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.asset_winners(&prefixes))
    }

    #[pyo3(signature = (mod_names, prefixes=Vec::new(), exact_paths=Vec::new(), extensions=Vec::new()))]
    fn asset_copies(
        &self,
        py: Python<'_>,
        mod_names: BTreeSet<String>,
        prefixes: Vec<String>,
        exact_paths: Vec<String>,
        extensions: Vec<String>,
    ) -> PyResult<Py<PyBytes>> {
        let snapshot = self.snapshot.clone();
        let bytes = py
            .detach(move || {
                let rows = snapshot.asset_copies(&mod_names, &prefixes, &exact_paths, &extensions);
                rmp_serde::to_vec(&rows).map_err(FileGraphError::from)
            })
            .map_err(PyErr::from)?;
        Ok(PyBytes::new(py, &bytes).unbind())
    }

    fn asset_winner_sources(&self, py: Python<'_>, prefixes: Vec<String>) -> PyResult<Py<PyBytes>> {
        let snapshot = self.snapshot.clone();
        let bytes = py
            .detach(move || {
                let rows = snapshot.asset_winner_sources(&prefixes);
                rmp_serde::to_vec(&rows).map_err(FileGraphError::from)
            })
            .map_err(PyErr::from)?;
        Ok(PyBytes::new(py, &bytes).unbind())
    }

    fn framework_basenames(
        &self,
        py: Python<'_>,
        mod_names: BTreeSet<String>,
    ) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.framework_basenames(&mod_names))
    }

    fn deployment_plan(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py_compact(py, &self.snapshot.deployment_plan())
    }

    fn data_entries(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py_compact(py, &self.snapshot.data_entries())
    }

    fn deployment_entries(
        &self,
        py: Python<'_>,
        candidate_ids: BTreeSet<i64>,
    ) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.deployment_entries(&candidate_ids))
    }

    fn contested_winner_ids(
        &self,
        py: Python<'_>,
        candidate_ids: BTreeSet<i64>,
    ) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.contested_winner_ids(&candidate_ids))
    }

    fn contested_paths(&self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.contested_paths())
    }

    fn winner(
        &self,
        py: Python<'_>,
        namespace: &str,
        target: &str,
        path: &[u8],
    ) -> PyResult<Py<PyBytes>> {
        encoded_py(
            py,
            &self
                .snapshot
                .winner(parse_namespace(namespace)?, target, path),
        )
    }

    fn providers(
        &self,
        py: Python<'_>,
        namespace: &str,
        target: &str,
        path: &[u8],
    ) -> PyResult<Py<PyBytes>> {
        encoded_py(
            py,
            &self
                .snapshot
                .providers(parse_namespace(namespace)?, target, path),
        )
    }

    fn conflict_summary(&self, py: Python<'_>, mod_name: &str) -> PyResult<Py<PyBytes>> {
        encoded_py(py, &self.snapshot.conflict_summary(mod_name))
    }

    #[pyo3(signature = (mod_name, kinds=Vec::new()))]
    fn conflict_partners(
        &self,
        py: Python<'_>,
        mod_name: &str,
        kinds: Vec<String>,
    ) -> PyResult<Py<PyBytes>> {
        let kinds: BTreeSet<String> = kinds.into_iter().collect();
        encoded_py(py, &self.snapshot.conflict_partners(mod_name, &kinds))
    }

    #[pyo3(signature = (first, second, kinds=Vec::new()))]
    fn conflict_files(
        &self,
        py: Python<'_>,
        first: &str,
        second: &str,
        kinds: Vec<String>,
    ) -> PyResult<Py<PyBytes>> {
        let kinds: BTreeSet<String> = kinds.into_iter().collect();
        encoded_py(py, &self.snapshot.conflict_files(first, second, &kinds))
    }

    fn archive_member_conflicts(
        &self,
        py: Python<'_>,
        mod_name: &str,
        source_rel: &[u8],
    ) -> PyResult<Py<PyBytes>> {
        encoded_py(
            py,
            &self.snapshot.archive_member_conflicts(mod_name, source_rel),
        )
    }
}

#[pyfunction]
fn api_version() -> u32 {
    API_VERSION
}

#[pyfunction]
fn schema_version() -> u32 {
    SCHEMA_VERSION
}

#[pymodule]
fn amethyst_filegraph(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CancelToken>()?;
    module.add_class::<LibrarySession>()?;
    module.add_class::<ProfileSession>()?;
    module.add_class::<ResolvedSnapshot>()?;
    module.add_function(wrap_pyfunction!(api_version, module)?)?;
    module.add_function(wrap_pyfunction!(schema_version, module)?)?;
    module.add("API_VERSION", API_VERSION)?;
    module.add("SCHEMA_VERSION", SCHEMA_VERSION)?;
    Ok(())
}
