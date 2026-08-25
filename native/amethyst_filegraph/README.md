# Amethyst filegraph

This crate owns Amethyst's durable content catalog and incremental conflict
graph. It is a required Python extension in packaged builds. The Python layer
is responsible for translating game-specific rules into per-mod manifest
batches; interactive enable, disable, reorder, and query operations stay in
Rust and do not call back into Python per file.

Build the stable-ABI extension with:

```bash
python native/amethyst_filegraph/build_extension.py
```

The script writes `src/amethyst_filegraph.abi3.so`, beside the LOOT extension.
That is the single location used by source runs and packaging. Generated Cargo
output and the extension itself are intentionally ignored by Git; CI rebuilds
the extension from the locked Rust sources before packaging.
