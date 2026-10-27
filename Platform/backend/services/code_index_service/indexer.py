"""Индексация файлов из локального repo-workspace → Postgres (текст) + Qdrant (векторы)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session

from services.code_index_service.models import CodeChunk
from services.code_index_service.qdrant_store import delete_scope, ensure_collection, upsert_chunks
from shared.rag_util import embed_many

logger = logging.getLogger("code_indexer")

WORKSPACE_ROOT = Path(os.getenv("REPO_WORKSPACE_ROOT", "/var/lambot/repo-workspaces")).resolve()
MAX_FILE_BYTES = int(os.getenv("CODE_INDEX_MAX_FILE_BYTES", "524288"))
CHUNK_LINES = int(os.getenv("CODE_INDEX_CHUNK_LINES", "80"))
CHUNK_OVERLAP = int(os.getenv("CODE_INDEX_CHUNK_OVERLAP", "12"))
MAX_FILES = int(os.getenv("CODE_INDEX_MAX_FILES", "8000"))
EMBED_BATCH = int(os.getenv("CODE_INDEX_EMBED_BATCH", "64"))

TEXT_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".sql",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".txt",
    ".sh",
    ".bash",
    ".zsh",
    ".toml",
    ".ini",
    ".cfg",
    ".env.example",
    ".vue",
    ".svelte",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".dockerfile",
    ".tf",
    ".hcl",
    ".graphql",
    ".css",
    ".scss",
    ".html",
    ".xml",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "target",
    ".idea",
    ".gradle",
    "vendor",
}


def _language_for(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in (".py", ".pyi"):
        return "python"
    if ext in (".ts", ".tsx"):
        return "typescript"
    if ext in (".js", ".jsx"):
        return "javascript"
    if ext:
        return ext.lstrip(".")
    return None


def _chunk_lines(text: str) -> list[str]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[str] = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    for i in range(0, len(lines), step):
        block = "\n".join(lines[i : i + CHUNK_LINES])
        if block.strip():
            chunks.append(block)
    return chunks


def _iter_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXTENSIONS and p.name not in ("Dockerfile", "Makefile"):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(p)
        if len(found) >= MAX_FILES:
            break
    return found


def index_workspace(
    db: Session,
    *,
    project_id: str,
    repo_slug: str,
    branch: str,
    commit_sha: str,
) -> dict:
    root = WORKSPACE_ROOT / project_id / repo_slug
    if not (root / ".git").is_dir():
        raise FileNotFoundError(f"workspace not cloned: {root}")

    delete_scope(project_id=project_id, repo_slug=repo_slug, branch=branch)
    db.execute(
        delete(CodeChunk).where(
            CodeChunk.project_id == project_id,
            CodeChunk.repo_slug == repo_slug,
            CodeChunk.branch == branch,
        )
    )
    db.flush()

    files = _iter_files(root)
    pending: list[tuple[str, str, int, str | None]] = []
    for fp in files:
        rel = fp.relative_to(root).as_posix()
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, ch in enumerate(_chunk_lines(text)):
            pending.append((rel, ch, idx, _language_for(rel)))

    stored = 0
    qdrant_rows: list[tuple[str, list[float], dict]] = []
    model_name: str | None = None
    provider_name: str | None = None

    for batch_start in range(0, len(pending), EMBED_BATCH):
        batch = pending[batch_start : batch_start + EMBED_BATCH]
        texts = [c[1] for c in batch]
        embeds, model, provider = embed_many(texts)
        if model:
            model_name = model
        if provider:
            provider_name = provider
        if embeds:
            ensure_collection(vector_size=len(embeds[0]))

        for i, (rel, ch, idx, lang) in enumerate(batch):
            cid = uuid4()
            point_id = str(cid)
            emb = embeds[i] if embeds and i < len(embeds) else None
            db.add(
                CodeChunk(
                    id=cid,
                    project_id=project_id,
                    repo_slug=repo_slug,
                    branch=branch,
                    commit_sha=commit_sha,
                    file_path=rel,
                    chunk_idx=idx,
                    language=lang,
                    chunk_text=ch,
                    char_count=len(ch),
                    qdrant_point_id=point_id,
                    embedding_model=model_name if emb is not None else None,
                    embedding_provider=provider_name if emb is not None else None,
                )
            )
            stored += 1
            if emb is not None:
                qdrant_rows.append(
                    (
                        point_id,
                        emb,
                        {
                            "chunk_id": point_id,
                            "file_path": rel,
                            "chunk_idx": idx,
                            "language": lang or "",
                        },
                    )
                )

    db.flush()
    qdrant_written = upsert_chunks(
        project_id=project_id,
        repo_slug=repo_slug,
        branch=branch,
        commit_sha=commit_sha,
        rows=qdrant_rows,
    )
    db.commit()
    logger.info(
        "indexed project=%s repo=%s branch=%s chunks=%s qdrant=%s files=%s",
        project_id,
        repo_slug,
        branch,
        stored,
        qdrant_written,
        len(files),
    )
    return {
        "files": len(files),
        "chunks": stored,
        "qdrant_points": qdrant_written,
        "branch": branch,
        "commit_sha": commit_sha,
        "vector_backend": "qdrant",
    }
