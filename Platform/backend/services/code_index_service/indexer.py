"""Индексация файлов из локального repo-workspace."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from services.code_index_service.models import CodeChunk
from shared.rag_util import embed_many

logger = logging.getLogger("code_indexer")

WORKSPACE_ROOT = Path(os.getenv("REPO_WORKSPACE_ROOT", "/var/lambot/repo-workspaces")).resolve()
MAX_FILE_BYTES = int(os.getenv("CODE_INDEX_MAX_FILE_BYTES", "524288"))
CHUNK_LINES = int(os.getenv("CODE_INDEX_CHUNK_LINES", "80"))
CHUNK_OVERLAP = int(os.getenv("CODE_INDEX_CHUNK_OVERLAP", "12"))
MAX_FILES = int(os.getenv("CODE_INDEX_MAX_FILES", "8000"))

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

    db.execute(
        delete(CodeChunk).where(
            CodeChunk.project_id == project_id,
            CodeChunk.repo_slug == repo_slug,
            CodeChunk.branch == branch,
        )
    )

    files = _iter_files(root)
    all_chunks: list[tuple[str, str, int, str | None]] = []
    for fp in files:
        rel = fp.relative_to(root).as_posix()
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, ch in enumerate(_chunk_lines(text)):
            all_chunks.append((rel, ch, idx, _language_for(rel)))

    texts = [c[1] for c in all_chunks]
    embeds, model, provider = embed_many(texts)

    stored = 0
    for i, (rel, ch, idx, lang) in enumerate(all_chunks):
        emb = embeds[i] if embeds and i < len(embeds) else None
        db.add(
            CodeChunk(
                project_id=project_id,
                repo_slug=repo_slug,
                branch=branch,
                commit_sha=commit_sha,
                file_path=rel,
                chunk_idx=idx,
                language=lang,
                chunk_text=ch,
                char_count=len(ch),
                embedding=emb,
                embedding_model=model if emb is not None else None,
                embedding_provider=provider if emb is not None else None,
            )
        )
        stored += 1
    db.commit()
    logger.info("indexed project=%s repo=%s branch=%s chunks=%s files=%s", project_id, repo_slug, branch, stored, len(files))
    return {"files": len(files), "chunks": stored, "branch": branch, "commit_sha": commit_sha}
