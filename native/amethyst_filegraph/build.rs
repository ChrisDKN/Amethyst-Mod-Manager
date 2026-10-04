use std::{env, fs, path::Path};

fn rules_revision(path: &Path, declaration: &str) -> u64 {
    println!("cargo:rerun-if-changed={}", path.display());
    let source = fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("Cannot read {}: {error}", path.display()));
    source
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once('=')?;
            (name.trim() == declaration).then_some(value.trim().trim_end_matches(';'))
        })
        .and_then(|value| value.parse().ok())
        .unwrap_or_else(|| panic!("Cannot read RULES_REVISION from {}", path.display()))
}

fn main() {
    let directory = env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is required");
    let directory = Path::new(&directory);
    let python = rules_revision(
        &directory.join("../../src/Utils/filegraph/native.py"),
        "RULES_REVISION",
    );
    let native = rules_revision(
        &directory.join("src/model.rs"),
        "pub const RULES_REVISION: u64",
    );
    assert_eq!(
        native, python,
        "Filegraph RULES_REVISION mismatch: update src/Utils/filegraph/native.py \
         and native/amethyst_filegraph/src/model.rs together"
    );
}
