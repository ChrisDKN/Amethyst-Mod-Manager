# Amethyst filegraph

This crate owns Amethyst's durable content catalog and incremental conflict
graph. It is a required Python extension in packaged builds. The Python layer
is responsible for translating game-specific rules into per-mod manifest
batches; interactive enable, disable, reorder, and query operations stay in
Rust and do not call back into Python per file.

`RULES_REVISION` is shared by Python's routing fingerprints and the native
catalog metadata. Update it in both `src/Utils/filegraph/native.py` and
`native/amethyst_filegraph/src/model.rs`; Cargo rejects mismatched values.
The reported revision identifies the component's rules contract, not whether
every stored profile has been refreshed. Profile reconciliation uses the
supplied rules fingerprints and routing-variant keys to detect changes.

Build the stable-ABI extension with:

```bash
native/amethyst_filegraph/build.sh
```

The equivalent Python build helper remains available as
`python native/amethyst_filegraph/build_extension.py`.

Building requires Cargo and a C toolchain. If `cc` is not installed on the
host, the script automatically uses the KDE 6.11 Flatpak SDK when it is
available (with the host's rustup-managed Cargo toolchain).

The script writes `src/amethyst_filegraph.abi3.so`, beside the LOOT extension.
That is the single location used by source runs and packaging. Generated Cargo
output and the extension itself are intentionally ignored by Git; CI rebuilds
the extension from the locked Rust sources before packaging.
