from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DEFAULT_LISTING_LIMIT = 3500


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def ensure_within_base(base_dir: Path, candidate: Path) -> Path:
    """Garantiza que candidate esté dentro de base_dir."""
    base_resolved = base_dir.resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved == base_resolved:
        return base_resolved
    if base_resolved in candidate_resolved.parents:
        return candidate_resolved
    raise ValueError(f"Ruta fuera del directorio base: {candidate_resolved}")


def safe_join(base_dir: Path, *parts: Iterable[str | Path]) -> Path:
    """Une partes y asegura que el resultado siga dentro del directorio base."""
    new_path = base_dir
    for part in parts:
        new_path = new_path / Path(part)
    return ensure_within_base(base_dir, new_path)


def chunk_numbered_lines(header: str, lines: List[str], limit: int = DEFAULT_LISTING_LIMIT) -> List[str]:
    """Divide un listado numerado en varios mensajes respetando el límite."""
    if not lines:
        return [header] if header else []

    messages: List[str] = []
    current = header + "\n" if header else ""

    for index, line in enumerate(lines, 1):
        numbered_line = f"{index}. {line}\n"
        if len(current) + len(numbered_line) > limit:
            messages.append(current.rstrip())
            current = numbered_line
        else:
            current += numbered_line

    if current.strip():
        messages.append(current.rstrip())

    return messages


def chunk_text(text: str, limit: int = DEFAULT_LISTING_LIMIT) -> List[str]:
    """Divide texto plano según el límite indicado."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks


INVALID_FILENAME_PATTERN = re.compile(r'[\\/:*?"<>|]+')


def sanitize_filename(name: str, expected_ext: Optional[str] = None, fallback: Optional[str] = None) -> str:
    """Normaliza un nombre de archivo eliminando caracteres inseguros y aplicando extensión."""
    candidate = INVALID_FILENAME_PATTERN.sub("_", name.strip())
    candidate = candidate.replace(" ", "_")
    if not candidate:
        if fallback:
            return fallback
        raise ValueError("Nombre de archivo vacío tras sanitizar.")

    if expected_ext:
        expected_ext = expected_ext if expected_ext.startswith(".") else f".{expected_ext}"
        lower_ext = expected_ext.lower()
        current_ext = Path(candidate).suffix.lower()
        if current_ext != lower_ext:
            if current_ext:
                # Mantiene la extensión proporcionada por el usuario.
                pass
            else:
                candidate = f"{candidate}{expected_ext}"

    return candidate
