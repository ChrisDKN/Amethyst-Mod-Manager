use pyo3::PyErr;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use std::path::PathBuf;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum FileGraphError {
    #[error("filegraph library is busy: {0}")]
    Busy(String),
    #[error("filegraph could not lock {path}: {source}")]
    Lock {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("filegraph catalog is corrupt: {0}")]
    Corrupt(String),
    #[error("filegraph catalog schema {found} is not supported (expected {expected})")]
    Schema { found: u32, expected: u32 },
    #[error("invalid filegraph input: {0}")]
    Invalid(String),
    #[error("filegraph database error: {0}")]
    Sql(#[from] rusqlite::Error),
    #[error("filegraph serialization error: {0}")]
    Encode(#[from] rmp_serde::encode::Error),
    #[error("filegraph deserialization error: {0}")]
    Decode(#[from] rmp_serde::decode::Error),
    #[error("filegraph I/O error: {0}")]
    Io(#[from] std::io::Error),
}

pub type Result<T> = std::result::Result<T, FileGraphError>;

impl From<FileGraphError> for PyErr {
    fn from(value: FileGraphError) -> Self {
        match value {
            FileGraphError::Invalid(message) => PyValueError::new_err(message),
            FileGraphError::Lock { path, source } => {
                let message = format!("Filegraph could not acquire the library lock: {source}");
                if let Some(errno) = source.raw_os_error() {
                    PyOSError::new_err((errno, message, path.to_string_lossy().into_owned()))
                } else {
                    PyOSError::new_err(format!("{message}: {}", path.display()))
                }
            }
            other => PyRuntimeError::new_err(other.to_string()),
        }
    }
}
