#!/usr/bin/env python3
"""
crosslink.py --vault VAULT_PATH

Vault-wide pass, not scoped to one book: reads every processed note
tracked in the manifest, finds Key Concepts terms that appear in notes
from more than one book, and adds a "## Cross-Book Links" section to
each involved note pointing at the others — separate from "## Related"
(same-book sibling links), so re-running is safe and idempotent.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src import manifest as manifest_mod
from src.crosslink import apply_cross_links

DEFAULT_VAULT_PATH = r"C:\Users\Marcus\Desktop\Textbook Notes Summarizer"


def main():
    parser = argparse.ArgumentParser(
        description="Cross-link notes across books that share Key Concepts terms.")
    parser.add_argument("--vault", default=DEFAULT_VAULT_PATH, help="Path to your Obsidian vault")
    parser.add_argument("--manifest", default=str(manifest_mod.DEFAULT_MANIFEST_PATH),
                         help="Path to the manifest JSON file")
    args = parser.parse_args()

    vault_root = Path(args.vault)
    manifest = manifest_mod.load_manifest(args.manifest)

    summary = apply_cross_links(vault_root, manifest)

    print(f"Scanned {summary['notes_scanned']} note(s) across the vault.")
    print(f"{summary['notes_linked']} note(s) updated with cross-book links "
          f"({summary['terms_matched']} shared term(s) found across 2+ books).")


if __name__ == "__main__":
    main()
