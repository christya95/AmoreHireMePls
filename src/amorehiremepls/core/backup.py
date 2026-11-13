"""Backup and restore of MongoDB collections + artifact directory + configuration, together.

Uses the driver (no mongodump dependency) with Extended JSON so types round-trip. Restore refuses to
write into a non-empty database unless forced.
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from bson import json_util

from amorehiremepls.core.db import Store
from amorehiremepls.core.util import sha256_file, utcnow

CONFIG_FILES = (".env.example", ".env.references.example", "docker-compose.yml", "config/pricing.json")


def _is_stop_record(parts: tuple[str, ...]) -> bool:
    """`jobs/<job_id>/stop/...` under the artifact folder."""
    return any(parts[i] == "jobs" and parts[i + 2] == "stop" for i in range(len(parts) - 2))


def _without_stop_records(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if _is_stop_record(tuple(Path(info.name).parts)) else info


def backup(store: Store, artifact_dir: Path, private_dir: Path, out_path: Path, repo_root: Path | None = None, include_checkpoints: bool = True) -> dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"created_at": utcnow().isoformat(), "database": store.db.name, "collections": {}, "artifacts": 0, "private": 0}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "db").mkdir()
        for name in store.db.list_collection_names():
            if not include_checkpoints and name.startswith("lg_"):
                continue
            # A stop record can hold a site password and session cookies. It never leaves the private data root.
            docs = list(store.db[name].find({"kind": {"$ne": "stop_record"}} if name == "artifacts" else {}))
            (tdp / "db" / f"{name}.json").write_text(json_util.dumps(docs), encoding="utf-8")
            manifest["collections"][name] = len(docs)
        (tdp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(tdp / "db", arcname="db")
            tar.add(tdp / "manifest.json", arcname="manifest.json")
            if artifact_dir.exists():
                tar.add(artifact_dir, arcname="artifacts", filter=_without_stop_records)
                manifest["artifacts"] = sum(1 for p in artifact_dir.rglob("*") if p.is_file() and not _is_stop_record(p.relative_to(artifact_dir).parts))
            if private_dir.exists():
                # sessions hold cookies: still backed up (the archive must be protected like the data itself)
                tar.add(private_dir, arcname="private")
                manifest["private"] = sum(1 for p in private_dir.rglob("*") if p.is_file())
            if repo_root:
                for rel in CONFIG_FILES:
                    p = repo_root / rel
                    if p.exists():
                        tar.add(p, arcname=f"config/{rel}")
    manifest["archive"] = str(out_path)
    manifest["sha256"] = sha256_file(out_path)
    return manifest


def restore(store: Store, archive: Path, artifact_dir: Path, private_dir: Path, force: bool = False) -> dict[str, Any]:
    existing = [n for n in store.db.list_collection_names() if store.db[n].estimated_document_count() > 0]
    if existing and not force:
        raise RuntimeError(f"target database {store.db.name} is not empty ({existing[:5]}); pass force=True to overwrite")
    restored: dict[str, Any] = {"collections": {}, "artifacts": 0, "private": 0}
    with tarfile.open(archive, "r:gz") as tar, tempfile.TemporaryDirectory() as td:
        tar.extractall(td, filter="data")
        tdp = Path(td)
        for f in sorted((tdp / "db").glob("*.json")):
            docs = json_util.loads(f.read_text(encoding="utf-8"))
            coll = store.db[f.stem]
            if force:
                coll.delete_many({})
            if docs:
                coll.insert_many(docs)
            restored["collections"][f.stem] = len(docs)
        for src, dst, key in ((tdp / "artifacts", artifact_dir, "artifacts"), (tdp / "private", private_dir, "private")):
            if src.exists():
                dst.mkdir(parents=True, exist_ok=True)
                for p in src.rglob("*"):
                    if p.is_file():
                        target = dst / p.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(p.read_bytes())
                        restored[key] += 1
    store.ensure_indexes()
    return restored
