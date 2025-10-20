#!/usr/bin/env python3
import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from file_browser import FileBrowser
from helpers import chunk_numbered_lines, chunk_text, ensure_within_base, sanitize_filename
from urllib.parse import quote, unquote

# ==========================
# CONFIGURACIÓN
# ==========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN", "TU TOKEN DE BOT DE TELEGRAM AQUÍ")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "TU ID AQUÍ"))
BASE_SAVE_PATH = Path(os.getenv("SAVE_PATH", "TU/DIRECTORIO/AQUÍ")).expanduser()
PICTURES_DIR = BASE_SAVE_PATH / "Pictures"
DOCUMENTS_DIR = BASE_SAVE_PATH / "Documents"
VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_PHOTO_SIZE_BYTES = 1 * 1024**3  # 1 GiB = 1_073_741_824 bytes

DELETE_CONTEXT_KEY = "file_ops:delete"
MOVE_CONTEXT_KEY = "file_ops:move"
RENAME_CONTEXT_KEY = "file_ops:rename"
GO_CONTEXT_KEY = "file_ops:go"

SCOPE_CONFIG = {
    "photos": {
        "base_dir": PICTURES_DIR,
        "emoji": "🖼️",
        "allowed_extensions": VALID_IMAGE_EXTENSIONS,
        "item_label": "imagen",
        "item_label_plural": "imágenes",
    },
    "docs": {
        "base_dir": DOCUMENTS_DIR,
        "emoji": "📄",
        "allowed_extensions": None,
        "item_label": "archivo",
        "item_label_plural": "archivos",
    },
}

for directory in (PICTURES_DIR, DOCUMENTS_DIR):
    directory.mkdir(parents=True, exist_ok=True)


# ==========================
# DECORADOR DE SEGURIDAD
# ==========================
def restricted(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message if update else None
        user = update.effective_user if update else None
        if not user:
            logger.debug("Llamada sin usuario.")
            return

        if user.id != AUTHORIZED_USER_ID:
            logger.warning("Acceso denegado para user_id=%s", user.id)
            if message:
                await message.reply_text("❌ No autorizado")
            return
        return await func(update, context)

    return wrapper


# ==========================
# UTILIDADES
# ==========================
async def send_safe_photo(update: Update, path: Path) -> None:
    """Intenta enviar una imagen y si falla, la manda como documento."""
    message = update.effective_message if update else None
    if not message:
        return

    if not path.exists():
        await message.reply_text(f"❌ El archivo no existe: {path.name}")
        return

    size = path.stat().st_size
    if size == 0:
        await message.reply_text(f"⚠️ El archivo {path.name} está vacío.")
        return

    caption = f"📸 {path.name}"
    try:
        if path.suffix.lower() in VALID_IMAGE_EXTENSIONS and size <= MAX_PHOTO_SIZE_BYTES:
            with path.open("rb") as fh:
                await message.reply_photo(photo=InputFile(fh, filename=path.name), caption=caption)
        else:
            with path.open("rb") as fh:
                await message.reply_document(
                    document=InputFile(fh, filename=path.name),
                    caption=f"📂 {path.name}",
                )
    except Exception as exc:  # noqa: BLE001 - necesitamos capturar cualquier error de envío
        logger.exception("Error enviando imagen %s: %s", path, exc)
        await message.reply_text(f"⚠️ No se pudo enviar como imagen ({exc}). Intentando como archivo...")
        try:
            with path.open("rb") as fh:
                await message.reply_document(
                    document=InputFile(fh, filename=path.name),
                    caption=f"📂 {path.name}",
                )
        except Exception as exc2:  # noqa: BLE001 - último recurso
            logger.exception("Error enviando documento %s: %s", path, exc2)
            await message.reply_text(f"❌ Error al enviar el archivo: {exc2}")


async def send_safe_document(update: Update, path: Path) -> None:
    message = update.effective_message if update else None
    if not message:
        return

    if not path.exists():
        await message.reply_text(f"❌ El archivo no existe: {path.name}")
        return

    size = path.stat().st_size
    if size == 0:
        await message.reply_text(f"⚠️ El archivo {path.name} está vacío.")
        return

    try:
        with path.open("rb") as fh:
            await message.reply_document(document=InputFile(fh, filename=path.name), caption=f"📂 {path.name}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error enviando documento %s: %s", path, exc)
        await message.reply_text(f"❌ Error al enviar el archivo: {exc}")


photo_browser = FileBrowser(
    namespace="photo",
    base_dir=PICTURES_DIR,
    send_entry=send_safe_photo,
    file_emoji="🖼️",
    item_label_singular="imagen",
    item_label_plural="imágenes",
    item_article="la",
    allowed_extensions=VALID_IMAGE_EXTENSIONS,
    selection_prompt="Elige una imagen tocando un botón o responde con /show <número> (o solo el número).",
    show_command="show",
    allow_text_commands=True,
)

document_browser = FileBrowser(
    namespace="docs",
    base_dir=DOCUMENTS_DIR,
    send_entry=send_safe_document,
    file_emoji="📄",
    item_label_singular="archivo",
    item_label_plural="archivos",
    item_article="el",
    allowed_extensions=None,
    selection_prompt="Elige un archivo tocando un botón o responde con /show <número> (o solo el número).",
    show_command="show",
    allow_text_commands=False,
)


def resolve_target_dir(
    context: ContextTypes.DEFAULT_TYPE,
    base_dir: Path,
    caption_tokens: List[str],
    group_id: Optional[str],
    cache_key_prefix: str,
) -> Path:
    """Resuelve el directorio destino según la caption y el grupo."""
    target_dir = base_dir
    cache_key = f"{cache_key_prefix}:{group_id}" if group_id else None

    if caption_tokens and caption_tokens[0] == "-f" and len(caption_tokens) >= 2:
        folder_name = " ".join(caption_tokens[1:])
        try:
            target_dir = ensure_within_base(base_dir, base_dir / folder_name)
        except ValueError:
            target_dir = base_dir
    elif cache_key and cache_key in context.chat_data:
        try:
            target_dir = ensure_within_base(base_dir, Path(context.chat_data[cache_key]))
        except ValueError:
            target_dir = base_dir

    target_dir.mkdir(parents=True, exist_ok=True)

    if cache_key and caption_tokens and caption_tokens[0] == "-f":
        context.chat_data[cache_key] = str(target_dir)

    return target_dir


def resolve_custom_name(
    caption_tokens: List[str],
    default_name: str,
    required_index: int,
    ext: str,
    raw_caption: Optional[str] = None,
) -> str:
    """Obtiene un nombre personalizado desde la caption si existe."""
    expected_ext = ext if ext else None
    sanitized_default = sanitize_filename(default_name, expected_ext, fallback=default_name)

    if caption_tokens:
        if caption_tokens[0] == "-f":
            if len(caption_tokens) > required_index:
                desired_raw = " ".join(caption_tokens[required_index:])
                return sanitize_filename(desired_raw, expected_ext, fallback=sanitized_default)
        else:
            desired_raw = raw_caption or " ".join(caption_tokens)
            return sanitize_filename(desired_raw, expected_ext, fallback=sanitized_default)

    return sanitized_default


def parse_command_arguments(text: Optional[str]) -> List[str]:
    if not text:
        return []

    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()

    if parts and parts[0].startswith("/"):
        parts = parts[1:]

    return parts


def find_matching_entries(
    base_dir: Path,
    needle: str,
    *,
    allowed_extensions: Optional[set[str]] = None,
    include_dirs: bool = True,
) -> List[Path]:
    if not needle:
        return []

    needle_lower = needle.lower()
    results: List[Path] = []

    for path in base_dir.rglob("*"):
        if path == base_dir:
            continue
        try:
            relative = path.relative_to(base_dir)
        except ValueError:
            continue

        name = relative.name.lower()
        if path.is_dir():
            if include_dirs and needle_lower in name:
                results.append(path)
        elif path.is_file():
            if allowed_extensions and path.suffix.lower() not in allowed_extensions:
                continue
            if needle_lower in name:
                results.append(path)

    results.sort(key=lambda p: str(p.relative_to(base_dir)).lower())
    return results


def format_entries_for_display(paths: List[Path], base_dir: Path, file_emoji: str) -> List[str]:
    lines = []
    for path in paths:
        relative = path.relative_to(base_dir)
        if path.is_dir():
            emoji = "📂"
        else:
            emoji = file_emoji
        lines.append(f"{emoji} {relative}")
    return lines


def store_delete_context(
    context: ContextTypes.DEFAULT_TYPE,
    scope: str,
    relatives: List[str],
    *,
    base_dir: Path,
    file_emoji: str,
) -> None:
    context.user_data[DELETE_CONTEXT_KEY] = {
        "scope": scope,
        "paths": relatives,
        "stage": "select",
        "base_dir": str(base_dir),
        "file_emoji": file_emoji,
    }


def clear_delete_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(DELETE_CONTEXT_KEY, None)


def build_index_keyboard(action: str, scope: str, count: int, row_size: int = 4) -> InlineKeyboardMarkup:
    if count <= 0:
        return InlineKeyboardMarkup([])

    rows: List[List[InlineKeyboardButton]] = []
    current_row: List[InlineKeyboardButton] = []

    for idx in range(count):
        current_row.append(
            InlineKeyboardButton(
                text=str(idx + 1),
                callback_data=f"OPS|{action}|{scope}|{idx}"
            )
        )
        if len(current_row) >= row_size:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    return InlineKeyboardMarkup(rows)


def delete_target_path(base_dir: Path, relative: str) -> Optional[str]:
    try:
        target = ensure_within_base(base_dir, base_dir / relative)
    except ValueError:
        return f"❌ Ruta fuera del directorio base: {relative}"

    if not target.exists():
        return f"❌ No existe: {relative}"

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error eliminando %s: %s", target, exc)
        return f"❌ Error eliminando {relative}: {exc}"

    return None


def get_delete_scope_base(scope: str) -> Path:
    if scope == "photos":
        return PICTURES_DIR
    if scope == "docs":
        return DOCUMENTS_DIR
    raise ValueError(f"Ámbito de borrado desconocido: {scope}")


async def prompt_delete_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    scope: str,
    base_dir: Path,
    relative: str,
    file_emoji: str,
) -> None:
    message = update.effective_message
    if not message:
        return

    path = base_dir / relative
    emoji = "📂" if path.is_dir() else file_emoji
    encoded = quote(relative, safe="")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Sí", callback_data=f"OPS|DEL|{scope}|YES|{encoded}"),
                InlineKeyboardButton("No", callback_data=f"OPS|DEL|{scope}|NO|{encoded}"),
            ]
        ]
    )
    context.user_data[DELETE_CONTEXT_KEY] = {
        "scope": scope,
        "pending": relative,
        "base_dir": str(base_dir),
        "file_emoji": file_emoji,
        "stage": "confirm",
    }
    await message.reply_text(
        f"¿Eliminar {emoji} {relative}? Esta acción no se puede deshacer.",
        reply_markup=keyboard,
    )


async def execute_delete_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    scope: str,
    base_dir: Path,
    allowed_extensions: Optional[set[str]],
    file_emoji: str,
    item_label: str,
) -> None:
    message = update.effective_message
    if not message:
        return

    clear_delete_context(context)

    args = parse_command_arguments(message.text)
    pattern = " ".join(args).strip()
    if not pattern:
        await message.reply_text(f"⚠️ Usa: /rm{'p' if scope == 'photos' else 'd'} <parte del nombre>")
        return

    matches = find_matching_entries(
        base_dir,
        pattern,
        allowed_extensions=allowed_extensions,
        include_dirs=True,
    )

    if not matches:
        await message.reply_text(f"❌ No encontré {item_label} que coincidan con '{pattern}'.")
        return

    relatives = [str(path.relative_to(base_dir)) for path in matches]

    if len(matches) == 1:
        await prompt_delete_confirmation(update, context, scope, base_dir, relatives[0], file_emoji)
        return

    store_delete_context(
        context,
        scope,
        relatives,
        base_dir=base_dir,
        file_emoji=file_emoji,
    )
    lines = format_entries_for_display(matches, base_dir, file_emoji)
    header = f"🔍 Existen {len(lines)} coincidencias:"
    for block in chunk_numbered_lines(header, lines):
        await message.reply_text(block)
    keyboard = build_index_keyboard("DELSEL", scope, len(relatives))
    await message.reply_text(
        "Responde con el número a eliminar o usa los botones (también 'cancelar').",
        reply_markup=keyboard,
    )


async def execute_move_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    base_dir: Path,
    scope_label: str,
) -> None:
    message = update.effective_message
    if not message:
        return

    args = parse_command_arguments(message.text)
    if len(args) != 2:
        await message.reply_text(f"⚠️ Usa: /mv{scope_label} <origen> <destino>")
        return

    src_arg, dest_arg = args
    error, final_relative = perform_move_operation(base_dir, src_arg, dest_arg)
    if error:
        await message.reply_text(error)
        return

    await message.reply_text(
        "📦 Movido:\n"
        f"{src_arg} → {final_relative}"
    )


async def execute_mkdir_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    base_dir: Path,
    scope_label: str,
) -> None:
    message = update.effective_message
    if not message:
        return

    args = parse_command_arguments(message.text)
    folder_arg = " ".join(args).strip()
    if not folder_arg:
        await message.reply_text(f"⚠️ Usa: /mkdir{scope_label} <nombre/directorio>")
        return

    try:
        target_dir = ensure_within_base(base_dir, base_dir / folder_arg)
    except ValueError:
        await message.reply_text(f"❌ Ruta fuera del directorio base: {folder_arg}")
        return

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error creando directorio %s: %s", target_dir, exc)
        await message.reply_text(f"❌ No se pudo crear la carpeta: {exc}")
        return

    await message.reply_text(f"📁 Carpeta creada: {target_dir.relative_to(base_dir)}")


# ==========================
# GUARDAR FOTOS
# ==========================
@restricted
async def save_img(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.photo:
        return

    raw_caption = message.caption.strip() if message.caption else ""
    caption_tokens = raw_caption.split() if raw_caption else []
    group_id = message.media_group_id
    largest_photo = message.photo[-1]

    target_dir = resolve_target_dir(context, PICTURES_DIR, caption_tokens, group_id, "photo_dir")
    default_name = f"foto_{largest_photo.file_unique_id}.jpg"
    photo_name = resolve_custom_name(
        caption_tokens,
        default_name,
        required_index=2,
        ext=".jpg",
        raw_caption=raw_caption,
    )

    try:
        file = await context.bot.get_file(largest_photo.file_id)
    except BadRequest as exc:
        if "File is too big" in exc.message:
            await message.reply_text(
                "❌ La foto supera el límite que impone Telegram (20 MB para bots)."
                " Por favor comprímela o envíala como documento dividido."
            )
            return
        raise
    file_path = target_dir / photo_name
    await file.download_to_drive(str(file_path))
    logger.info("Foto guardada: %s", file_path)

    last_group_key = "last_photo_group_id"
    should_notify = not group_id or context.chat_data.get(last_group_key) != group_id
    if should_notify:
        await message.reply_text(f"🖼️ Fotos guardadas en:\n{target_dir}")
        if group_id:
            context.chat_data[last_group_key] = group_id


# ==========================
# GUARDAR DOCUMENTOS
# ==========================
@restricted
async def save_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    document = message.document if message else None
    if not document:
        return

    raw_caption = message.caption.strip() if message.caption else ""
    caption_tokens = raw_caption.split() if raw_caption else []
    group_id = message.media_group_id

    original_name = Path(document.file_name).name if document.file_name else ""
    ext = Path(original_name).suffix if original_name else ""
    fallback_default = f"doc_{document.file_unique_id}{ext}"
    if original_name:
        default_name = sanitize_filename(original_name, ext or None, fallback=fallback_default)
    else:
        default_name = sanitize_filename(fallback_default, ext or None, fallback=fallback_default)

    target_dir = resolve_target_dir(context, DOCUMENTS_DIR, caption_tokens, group_id, "doc_dir")
    doc_name = resolve_custom_name(
        caption_tokens,
        default_name,
        required_index=2,
        ext=ext,
        raw_caption=raw_caption,
    )

    try:
        file = await context.bot.get_file(document.file_id)
    except BadRequest as exc:
        if "File is too big" in exc.message:
            await message.reply_text(
                "❌ El archivo excede el máximo permitido por Telegram para bots (≈50 MB)."
                " Divide o comprímelo antes de reenviarlo."
            )
            return
        raise
    file_path = target_dir / doc_name
    await file.download_to_drive(str(file_path))
    logger.info("Documento guardado: %s", file_path)

    last_group_key = "last_doc_group_id"
    should_notify = not group_id or context.chat_data.get(last_group_key) != group_id
    if should_notify:
        await message.reply_text(f"📂 Archivos guardados en:\n{target_dir}")
        if group_id:
            context.chat_data[last_group_key] = group_id


# ==========================
# /showp y /showd
# ==========================
@restricted
async def showp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await photo_browser.handle_list(update, context)


@restricted
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await photo_browser.handle_list(update, context)


@restricted
async def list_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await photo_browser.handle_list(update, context)


@restricted
async def list_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await document_browser.handle_list(update, context)


@restricted
async def show_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    active_browser = context.user_data.get(FileBrowser.ACTIVE_KEY)
    if active_browser == document_browser.namespace:
        await document_browser.handle_show(update, context, query)
    else:
        await photo_browser.handle_show(update, context, query)


@restricted
async def go_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = " ".join(context.args).strip()
    clear_go_context(context)
    await photo_browser.handle_go(update, context, target)


@restricted
async def go_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        clear_go_context(context)
        await photo_browser.handle_list(update, context)
        return

    await handle_partial_go_command(update, context, "photos", photo_browser, query)


@restricted
async def go_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        clear_go_context(context)
        await document_browser.handle_list(update, context)
        return

    await handle_partial_go_command(update, context, "docs", document_browser, query)


@restricted
async def file_browser_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await photo_browser.handle_callback(update, context):
        return
    if await document_browser.handle_callback(update, context):
        return
    if update.callback_query:
        await update.callback_query.answer("Acción no reconocida.", show_alert=True)


@restricted
async def operations_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    if not parts or parts[0] != "OPS":
        await query.answer("Acción no reconocida.", show_alert=True)
        return

    action = parts[1]

    if action == "DEL":
        if len(parts) < 5:
            await query.answer("Datos inválidos", show_alert=True)
            return
        scope, decision, encoded_relative = parts[2], parts[3], parts[4]
        relative = unquote(encoded_relative)
        await query.answer()
        try:
            base_dir = get_delete_scope_base(scope)
        except ValueError:
            clear_delete_context(context)
            await query.edit_message_text("❌ Contexto de eliminación inválido.")
            return

        if decision == "YES":
            error = delete_target_path(base_dir, relative)
            if error:
                await query.edit_message_text(error)
            else:
                await query.edit_message_text(f"🗑️ Eliminado: {relative}")
        else:
            await query.edit_message_text("Operación cancelada.")

        clear_delete_context(context)
        return

    if action == "DELSEL":
        if len(parts) < 4:
            await query.answer("Datos inválidos", show_alert=True)
            return
        scope, index_str = parts[2], parts[3]
        delete_ctx = context.user_data.get(DELETE_CONTEXT_KEY)
        if not delete_ctx or delete_ctx.get("scope") != scope:
            clear_delete_context(context)
            await query.answer("Sin contexto", show_alert=True)
            return
        try:
            idx = int(index_str)
        except ValueError:
            await query.answer("Índice inválido", show_alert=True)
            return

        paths: List[str] = delete_ctx.get("paths", [])
        if not (0 <= idx < len(paths)):
            await query.answer("Índice fuera de rango", show_alert=True)
            return

        base_dir_str = delete_ctx.get("base_dir")
        file_emoji = delete_ctx.get("file_emoji", "📄")
        try:
            base_dir = Path(base_dir_str) if base_dir_str else get_delete_scope_base(scope)
        except ValueError:
            clear_delete_context(context)
            await query.answer("Contexto inválido", show_alert=True)
            return
        relative = paths[idx]
        await query.answer()
        await query.edit_message_reply_markup(None)
        await query.edit_message_text(f"Seleccionado: {relative}")
        await prompt_delete_confirmation(update, context, scope, base_dir, relative, file_emoji)
        return

    if action in {"MOVSRC", "MOVDST"}:
        if len(parts) < 4:
            await query.answer("Datos inválidos", show_alert=True)
            return
        scope, index_str = parts[2], parts[3]
        move_ctx = context.user_data.get(MOVE_CONTEXT_KEY)
        expected_stage = "await_origin_choice" if action == "MOVSRC" else "await_destination_choice"
        if not move_ctx or move_ctx.get("scope") != scope or move_ctx.get("stage") != expected_stage:
            await query.answer("Sin contexto", show_alert=True)
            return
        try:
            idx = int(index_str)
        except ValueError:
            await query.answer("Índice inválido", show_alert=True)
            return

        candidates: List[str] = move_ctx.get("candidates", [])
        if not (0 <= idx < len(candidates)):
            await query.answer("Índice fuera de rango", show_alert=True)
            return

        await query.answer()
        await query.edit_message_reply_markup(None)
        await query.edit_message_text(f"Seleccionaste: {candidates[idx]}")

        # Reutiliza el flujo de texto enviando el número correspondiente
        await process_move_flow(update, context, str(idx + 1))
        return

    if action == "GOSEL":
        if len(parts) < 4:
            await query.answer("Datos inválidos", show_alert=True)
            return
        scope, index_str = parts[2], parts[3]
        go_ctx = context.user_data.get(GO_CONTEXT_KEY)
        if not go_ctx or go_ctx.get("scope") != scope or go_ctx.get("stage") != "select":
            await query.answer("Sin contexto", show_alert=True)
            return
        try:
            idx = int(index_str)
        except ValueError:
            await query.answer("Índice inválido", show_alert=True)
            return

        candidates: List[str] = go_ctx.get("candidates", [])
        if not (0 <= idx < len(candidates)):
            await query.answer("Índice fuera de rango", show_alert=True)
            return

        await query.answer()
        await query.edit_message_reply_markup(None)
        await query.edit_message_text(f"Carpeta seleccionada: {candidates[idx]}/")
        await apply_go_selection(scope, candidates[idx], update, context)
        return

    if action == "RENSEL":
        if len(parts) < 4:
            await query.answer("Datos inválidos", show_alert=True)
            return
        scope, index_str = parts[2], parts[3]
        rename_ctx = context.user_data.get(RENAME_CONTEXT_KEY)
        if not rename_ctx or rename_ctx.get("scope") != scope or rename_ctx.get("stage") != "await_target_choice":
            await query.answer("Sin contexto", show_alert=True)
            return
        try:
            idx = int(index_str)
        except ValueError:
            await query.answer("Índice inválido", show_alert=True)
            return

        candidates: List[str] = rename_ctx.get("candidates", [])
        if not (0 <= idx < len(candidates)):
            await query.answer("Índice fuera de rango", show_alert=True)
            return

        await query.answer()
        await query.edit_message_reply_markup(None)
        await query.edit_message_text(f"Archivo seleccionado: {candidates[idx]}")
        await process_rename_flow(update, context, str(idx + 1))
        return

    await query.answer("Acción no reconocida.", show_alert=True)
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    try:
        result = subprocess.run(
            ["tailscale", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        await message.reply_text("❌ tailscale no está disponible en este servidor.")
        return

    output = result.stdout.strip()
    error_output = result.stderr.strip()

    if result.returncode != 0:
        content = output or error_output or f"Error (código {result.returncode})"
        prefix = "⚠️ tailscale status falló:\n"
    else:
        content = output or "(sin salida)"
        prefix = "📡 tailscale status:\n"

    chunks = chunk_text(content, 3500)
    for idx, chunk in enumerate(chunks):
        header = prefix if idx == 0 else ""
        await message.reply_text(f"{header}{chunk}" if header else chunk)


@restricted
async def rm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    args = parse_command_arguments(message.text)
    if not args:
        await message.reply_text("⚠️ Usa: /rm <ruta_relativa> [...]")
        return

    removed: List[str] = []
    errors: List[str] = []

    for arg in args:
        try:
            target = ensure_within_base(BASE_SAVE_PATH, BASE_SAVE_PATH / arg)
        except ValueError:
            errors.append(f"❌ Ruta fuera del directorio base: {arg}")
            continue

        if target == BASE_SAVE_PATH:
            errors.append("❌ No se puede eliminar la carpeta base.")
            continue

        relative = str(target.relative_to(BASE_SAVE_PATH))
        error = delete_target_path(BASE_SAVE_PATH, relative)
        if error:
            errors.append(error)
        else:
            removed.append(relative)

    if removed:
        await message.reply_text("🗑️ Eliminado:\n" + "\n".join(f"- {path}" for path in removed))

    if errors:
        await message.reply_text("\n".join(errors))


@restricted
async def rmp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await execute_delete_command(
        update,
        context,
        scope="photos",
        base_dir=PICTURES_DIR,
        allowed_extensions=VALID_IMAGE_EXTENSIONS,
        file_emoji="🖼️",
        item_label="fotos o carpetas",
    )


@restricted
async def rmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await execute_delete_command(
        update,
        context,
        scope="docs",
        base_dir=DOCUMENTS_DIR,
        allowed_extensions=None,
        file_emoji="📄",
        item_label="archivos o carpetas",
    )


@restricted
async def mv_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    args = parse_command_arguments(message.text)
    if len(args) != 2:
        await message.reply_text("⚠️ Usa: /mv <origen> <destino>")
        return

    src_arg, dest_arg = args

    error, final_relative = perform_move_operation(BASE_SAVE_PATH, src_arg, dest_arg)
    if error:
        await message.reply_text(error)
        return

    await message.reply_text(
        "📦 Movido:\n"
        f"{src_arg} → {final_relative}"
    )


def perform_move_operation(base_dir: Path, src_relative: str, dest_relative: str) -> tuple[Optional[str], Optional[str]]:
    try:
        src_path = ensure_within_base(base_dir, base_dir / src_relative)
    except ValueError:
        return (f"❌ Ruta fuera del directorio base: {src_relative}", None)

    if not src_path.exists():
        return (f"❌ Origen no existe: {src_relative}", None)

    if src_path == base_dir:
        return ("❌ No se puede mover el directorio base.", None)

    try:
        dest_candidate = ensure_within_base(base_dir, base_dir / dest_relative)
    except ValueError:
        return (f"❌ Ruta fuera del directorio base: {dest_relative}", None)

    if dest_candidate == base_dir or (dest_candidate.exists() and dest_candidate.is_dir()):
        final_dest = ensure_within_base(base_dir, dest_candidate / src_path.name)
    else:
        final_dest = dest_candidate

    if final_dest == src_path:
        return ("⚠️ El destino es igual al origen.", None)

    if final_dest.exists():
        if final_dest.is_dir() and src_path.is_dir():
            return ("❌ Ya existe un directorio con ese nombre en el destino.", None)
        if final_dest.is_file():
            return ("❌ Ya existe un archivo con ese nombre en el destino.", None)

    if not final_dest.parent.exists():
        return ("❌ El directorio destino no existe.", None)

    try:
        shutil.move(str(src_path), str(final_dest))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error moviendo %s a %s: %s", src_path, final_dest, exc)
        return (f"❌ Error moviendo: {exc}", None)

    return (None, str(final_dest.relative_to(base_dir)))


def perform_rename_operation(
    base_dir: Path,
    src_relative: str,
    new_name: str,
) -> tuple[Optional[str], Optional[str]]:
    try:
        src_path = ensure_within_base(base_dir, base_dir / src_relative)
    except ValueError:
        return (f"❌ Ruta fuera del directorio base: {src_relative}", None)

    if not src_path.exists():
        return (f"❌ No existe: {src_relative}", None)

    if src_path.is_dir():
        return ("❌ Solo se pueden renombrar archivos.", None)

    ext = src_path.suffix
    sanitized = sanitize_filename(new_name, ext or None, fallback=src_path.name)
    if not sanitized:
        return ("❌ Nombre inválido.", None)

    dest_path = src_path.with_name(sanitized)

    try:
        dest_path = ensure_within_base(base_dir, dest_path)
    except ValueError:
        return ("❌ El nuevo nombre sale del directorio permitido.", None)

    if dest_path == src_path:
        return ("⚠️ El nuevo nombre es igual al actual.", None)

    if dest_path.exists():
        return ("❌ Ya existe un archivo con ese nombre.", None)

    try:
        src_path.rename(dest_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error renombrando %s a %s: %s", src_path, dest_path, exc)
        return (f"❌ Error renombrando: {exc}", None)

    return (None, str(dest_path.relative_to(base_dir)))


def clear_move_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(MOVE_CONTEXT_KEY, None)


def clear_rename_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(RENAME_CONTEXT_KEY, None)


def clear_go_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(GO_CONTEXT_KEY, None)


def store_go_context(context: ContextTypes.DEFAULT_TYPE, scope: str, candidates: List[str]) -> None:
    context.user_data[GO_CONTEXT_KEY] = {
        "scope": scope,
        "candidates": candidates,
        "stage": "select",
    }


async def apply_go_selection(scope: str, candidate: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    browser = photo_browser if scope == "photos" else document_browser
    await browser.handle_go(update, context, candidate)
    clear_go_context(context)


async def start_move_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str) -> None:
    message = update.effective_message
    if not message:
        return

    clear_move_context(context)
    config = SCOPE_CONFIG[scope]
    base_dir: Path = config["base_dir"]
    context.user_data[MOVE_CONTEXT_KEY] = {
        "scope": scope,
        "stage": "await_origin_input",
    }
    await message.reply_text(
        f"Envía parte del nombre del origen en {base_dir} (o escribe 'cancelar')."
    )


async def start_rename_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str) -> None:
    message = update.effective_message
    if not message:
        return

    clear_rename_context(context)
    config = SCOPE_CONFIG[scope]
    base_dir: Path = config["base_dir"]
    context.user_data[RENAME_CONTEXT_KEY] = {
        "scope": scope,
        "stage": "await_target_input",
    }
    await message.reply_text(
        f"Envía parte del nombre del archivo en {base_dir} que quieres renombrar "
        "(o escribe 'cancelar')."
    )


async def handle_partial_go_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    scope: str,
    browser: FileBrowser,
    query: str,
) -> None:
    message = update.effective_message
    if not message:
        return

    clear_go_context(context)

    lowered = query.lower()
    if lowered in {"..", "../"}:
        clear_go_context(context)
        await browser.handle_go(update, context, "..")
        return

    if lowered in {".", ""}:
        clear_go_context(context)
        await browser.handle_list(update, context)
        return

    current_path = browser.get_current_path(context)
    matches = [
        child
        for child in sorted(current_path.iterdir(), key=lambda p: p.name.lower())
        if child.is_dir() and lowered in child.name.lower()
    ]

    if not matches:
        await message.reply_text("❌ No encontré carpetas con ese nombre.")
        clear_go_context(context)
        return

    if len(matches) == 1:
        clear_go_context(context)
        await browser.handle_go(update, context, matches[0].name)
        return

    candidates = [child.name for child in matches]
    store_go_context(context, scope, candidates)

    lines = [f"📂 {name}/" for name in candidates]
    header = f"🔍 Coincidencias encontradas ({len(lines)}):"
    for block in chunk_numbered_lines(header, lines):
        await message.reply_text(block)

    keyboard = build_index_keyboard("GOSEL", scope, len(candidates))
    await message.reply_text(
        "Elige la carpeta con los botones o responde con el número (o 'cancelar').",
        reply_markup=keyboard,
    )

async def process_move_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    move_ctx = context.user_data.get(MOVE_CONTEXT_KEY)
    if not move_ctx:
        return False

    message = update.effective_message
    if not message:
        return True

    lower = text.lower()
    if lower in {"cancel", "cancelar", "salir", "stop"}:
        clear_move_context(context)
        await message.reply_text("Operación de mover cancelada.")
        return True

    scope = move_ctx.get("scope", "photos")
    if scope not in SCOPE_CONFIG:
        clear_move_context(context)
        await message.reply_text("❌ Contexto de movimiento inválido.")
        return True

    config = SCOPE_CONFIG[scope]
    base_dir: Path = config["base_dir"]
    file_emoji: str = config["emoji"]
    allowed_ext = config["allowed_extensions"]
    stage = move_ctx.get("stage")

    if stage == "await_origin_choice":
        candidates: List[str] = move_ctx.get("candidates", [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(candidates):
                move_ctx["origin"] = candidates[idx - 1]
                move_ctx["stage"] = "await_destination_input"
                move_ctx.pop("candidates", None)
                await message.reply_text(
                    "Ahora envía parte del nombre del destino (carpeta) "
                    f"en {base_dir} (usa 'cancelar' para interrumpir; '.' para la carpeta raíz)."
                )
            else:
                await message.reply_text("⚠️ Número fuera de rango.")
        else:
            await message.reply_text("❌ Escribe un número válido o 'cancelar'.")
        return True

    if stage == "await_destination_choice":
        candidates: List[str] = move_ctx.get("candidates", [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(candidates):
                dest_relative = candidates[idx - 1]
                origin_relative = move_ctx.get("origin")
                move_ctx.pop("candidates", None)
                if not origin_relative:
                    clear_move_context(context)
                    await message.reply_text("❌ No se definió un origen válido.")
                    return True
                error, final_relative = perform_move_operation(base_dir, origin_relative, dest_relative)
                clear_move_context(context)
                if error:
                    await message.reply_text(error)
                else:
                    await message.reply_text(
                        "📦 Movido:\n"
                        f"{origin_relative} → {final_relative}"
                    )
            else:
                await message.reply_text("⚠️ Número fuera de rango.")
        else:
            await message.reply_text("❌ Escribe un número válido o 'cancelar'.")
        return True

    if stage == "await_origin_input":
        matches = find_matching_entries(
            base_dir,
            text,
            allowed_extensions=allowed_ext,
            include_dirs=True,
        )
        if not matches:
            await message.reply_text("❌ No encontré coincidencias para el origen.")
            return True

        relatives = [str(p.relative_to(base_dir)) for p in matches]
        if len(relatives) == 1:
            move_ctx["origin"] = relatives[0]
            move_ctx["stage"] = "await_destination_input"
            await message.reply_text(
                "Origen seleccionado. Envía parte del nombre del destino (carpeta) "
                f"en {base_dir} (o escribe 'cancelar'; usa '.' para la carpeta raíz)."
            )
            return True

        move_ctx["candidates"] = relatives
        move_ctx["stage"] = "await_origin_choice"
        lines = format_entries_for_display(matches, base_dir, file_emoji)
        header = f"🔍 Coincidencias para el origen ({len(lines)}):"
        for block in chunk_numbered_lines(header, lines):
            await message.reply_text(block)
        keyboard = build_index_keyboard("MOVSRC", scope, len(relatives))
        await message.reply_text(
            "Responde con el número del origen deseado o usa los botones (también 'cancelar').",
            reply_markup=keyboard,
        )
        return True

    if stage == "await_destination_input":
        if text == ".":
            dest_relative = "."
            origin_relative = move_ctx.get("origin")
            if not origin_relative:
                clear_move_context(context)
                await message.reply_text("❌ No se definió un origen válido.")
                return True
            error, final_relative = perform_move_operation(base_dir, origin_relative, dest_relative)
            clear_move_context(context)
            if error:
                await message.reply_text(error)
            else:
                await message.reply_text(
                    "📦 Movido:\n"
                    f"{origin_relative} → {final_relative}"
                )
            return True

        matches = [
            p
            for p in find_matching_entries(base_dir, text, allowed_extensions=None, include_dirs=True)
            if p.is_dir()
        ]

        if not matches:
            await message.reply_text("❌ No encontré coincidencias para el destino. Intenta otra vez.")
            return True

        relatives = [str(p.relative_to(base_dir)) for p in matches]
        if len(relatives) == 1:
            origin_relative = move_ctx.get("origin")
            if not origin_relative:
                clear_move_context(context)
                await message.reply_text("❌ No se definió un origen válido.")
                return True
            error, final_relative = perform_move_operation(base_dir, origin_relative, relatives[0])
            clear_move_context(context)
            if error:
                await message.reply_text(error)
            else:
                await message.reply_text(
                    "📦 Movido:\n"
                    f"{origin_relative} → {final_relative}"
                )
            return True

        move_ctx["candidates"] = relatives
        move_ctx["stage"] = "await_destination_choice"
        lines = format_entries_for_display(matches, base_dir, file_emoji)
        header = f"🔍 Destinos posibles ({len(lines)}):"
        for block in chunk_numbered_lines(header, lines):
            await message.reply_text(block)
        keyboard = build_index_keyboard("MOVDST", scope, len(relatives))
        await message.reply_text(
            "Responde con el número del destino deseado o usa los botones (también 'cancelar').",
            reply_markup=keyboard,
        )
        return True

    return True


async def process_rename_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    rename_ctx = context.user_data.get(RENAME_CONTEXT_KEY)
    if not rename_ctx:
        return False

    message = update.effective_message
    if not message:
        return True

    lower = text.lower()
    if lower in {"cancel", "cancelar", "salir", "stop"}:
        clear_rename_context(context)
        await message.reply_text("Operación de renombrar cancelada.")
        return True

    scope = rename_ctx.get("scope", "photos")
    if scope not in SCOPE_CONFIG:
        clear_rename_context(context)
        await message.reply_text("❌ Contexto de renombrado inválido.")
        return True

    config = SCOPE_CONFIG[scope]
    base_dir: Path = config["base_dir"]
    file_emoji: str = config["emoji"]
    allowed_ext = config["allowed_extensions"]
    stage = rename_ctx.get("stage")

    if stage == "await_target_choice":
        candidates: List[str] = rename_ctx.get("candidates", [])
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(candidates):
                rename_ctx["target"] = candidates[idx - 1]
                rename_ctx["stage"] = "await_new_name"
                rename_ctx.pop("candidates", None)
                await message.reply_text(
                    "Escribe el nuevo nombre (sin ruta). Mantendremos la extensión original si no especificas una."
                )
            else:
                await message.reply_text("⚠️ Número fuera de rango.")
        else:
            await message.reply_text("❌ Escribe un número válido o 'cancelar'.")
        return True

    if stage == "await_target_input":
        matches = [
            p
            for p in find_matching_entries(
                base_dir,
                text,
                allowed_extensions=allowed_ext,
                include_dirs=False,
            )
        ]

        if not matches:
            await message.reply_text("❌ No encontré archivos con ese nombre.")
            return True

        relatives = [str(p.relative_to(base_dir)) for p in matches]
        if len(relatives) == 1:
            rename_ctx["target"] = relatives[0]
            rename_ctx["stage"] = "await_new_name"
            await message.reply_text(
                "Origen seleccionado. Escribe el nuevo nombre (sin ruta)."
                " Mantendremos la extensión original si no especificas una."
            )
            return True

        rename_ctx["candidates"] = relatives
        rename_ctx["stage"] = "await_target_choice"
        lines = format_entries_for_display(matches, base_dir, file_emoji)
        header = f"🔍 Coincidencias encontradas ({len(lines)}):"
        for block in chunk_numbered_lines(header, lines):
            await message.reply_text(block)
        keyboard = build_index_keyboard("RENSEL", scope, len(relatives))
        await message.reply_text(
            "Responde con el número del archivo a renombrar o usa los botones (también 'cancelar').",
            reply_markup=keyboard,
        )
        return True

    if stage == "await_new_name":
        target_relative = rename_ctx.get("target")
        if not target_relative:
            clear_rename_context(context)
            await message.reply_text("❌ No se definió un archivo a renombrar.")
            return True

        error, new_relative = perform_rename_operation(base_dir, target_relative, text)
        clear_rename_context(context)
        if error:
            await message.reply_text(error)
        else:
            await message.reply_text(
                "🔤 Renombrado:\n"
                f"{target_relative} → {new_relative}"
            )
        return True

    return True


@restricted
async def mvp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_move_flow(update, context, "photos")


@restricted
async def mvd_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_move_flow(update, context, "docs")


@restricted
async def rename_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_rename_flow(update, context, "photos")


@restricted
async def rename_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_rename_flow(update, context, "docs")


@restricted
async def mkdir_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await execute_mkdir_command(
        update,
        context,
        base_dir=PICTURES_DIR,
        scope_label="p",
    )


@restricted
async def mkdir_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await execute_mkdir_command(
        update,
        context,
        base_dir=DOCUMENTS_DIR,
        scope_label="d",
    )


@restricted
async def showd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if query:
        await document_browser.handle_show(update, context, query)
    else:
        await document_browser.handle_list(update, context)


# ==========================
# RESPUESTAS DEL USUARIO + MINIATURAS
# ==========================
@restricted
async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()

    delete_ctx = context.user_data.get(DELETE_CONTEXT_KEY)
    if delete_ctx:
        lower = text.lower()
        stage = delete_ctx.get("stage")
        if lower in {"cancel", "cancelar", "salir", "stop"}:
            clear_delete_context(context)
            await message.reply_text("Operación cancelada.")
            return

        if stage == "select":
            if text.isdigit():
                idx = int(text)
                paths = delete_ctx.get("paths", [])
                if 1 <= idx <= len(paths):
                    scope = delete_ctx.get("scope", "")
                    base_dir_str = delete_ctx.get("base_dir")
                    try:
                        base_dir = Path(base_dir_str) if base_dir_str else get_delete_scope_base(scope)
                    except ValueError:
                        clear_delete_context(context)
                        await message.reply_text("❌ Contexto de eliminación inválido.")
                        return
                    file_emoji = delete_ctx.get("file_emoji", "📄")
                    relative = paths[idx - 1]
                    await prompt_delete_confirmation(update, context, scope, base_dir, relative, file_emoji)
                else:
                    await message.reply_text("⚠️ Número fuera de rango.")
            else:
                await message.reply_text("❌ Escribe un número válido o 'cancelar'.")
            return

        if stage == "confirm":
            await message.reply_text("Usa los botones de confirmación para continuar.")
            return

    go_ctx = context.user_data.get(GO_CONTEXT_KEY)
    if go_ctx:
        lower = text.lower()
        if lower in {"cancel", "cancelar", "salir", "stop"}:
            clear_go_context(context)
            await message.reply_text("Operación cancelada.")
            return

        if go_ctx.get("stage") != "select":
            clear_go_context(context)
            await message.reply_text("Contexto de navegación inválido. Intenta nuevamente.")
            return

        if text.isdigit():
            idx = int(text)
            candidates: List[str] = go_ctx.get("candidates", [])
            if 1 <= idx <= len(candidates):
                scope = go_ctx.get("scope", "photos")
                await apply_go_selection(scope, candidates[idx - 1], update, context)
            else:
                await message.reply_text("⚠️ Número fuera de rango.")
        else:
            await message.reply_text("❌ Escribe un número válido o usa los botones.")
        return

    if await process_move_flow(update, context, text):
        return

    if await process_rename_flow(update, context, text):
        return

    if await document_browser.process_text(update, context, text):
        return

    if await photo_browser.process_text(update, context, text):
        return

    await message.reply_text("⚠️ No entendí. Usa /listp, /listd, /show, /showd o un número válido.")


# ==========================
# COMANDOS BÁSICOS
# ==========================
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🤖 Bot de control activado.\n\nComandos:\n"
        "/listp – Listar fotos y carpetas\n"
        "/listd – Listar documentos y carpetas\n"
        "/show <nombre> – Mostrar imagen o selección activa\n"
        "/showd <nombre> – Buscar y mostrar documento\n"
        "/gop <dir> – Cambiar de carpeta de fotos\n"
        "/god <dir> – Cambiar de carpeta de documentos\n"
        "/rmp <nombre> – Eliminar fotos o carpetas\n"
        "/rmd <nombre> – Eliminar documentos o carpetas\n"
        "/mvp – Mover fotos o carpetas mediante asistente\n"
        "/mvd – Mover documentos o carpetas mediante asistente\n"
        "/renamep – Renombrar foto\n"
        "/renamed – Renombrar documento\n"
        "/mkdirp <ruta> – Crear carpeta de fotos\n"
        "/mkdird <ruta> – Crear carpeta de documentos\n"
        "/status – Mostrar estado de tailscale\n"
        "/hora – Ver hora actual\n"
        "/reboot – Reiniciar servidor"
    )


@restricted
async def hora(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hora_actual = os.popen("date").read().strip()
    await update.effective_message.reply_text(f"🕓 Hora actual:\n{hora_actual}")


@restricted
async def reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("🔁 Reiniciando servidor...")
    os.system("sudo reboot")


@restricted
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    command = message.text.split()[0]
    await message.reply_text(
        f"❌ Comando no reconocido: {command}. Usa /start para ver la lista disponible."
    )


async def on_startup(app: Application) -> None:
    await app.bot.send_message(chat_id=AUTHORIZED_USER_ID, text="✅ Servidor prendido")

# ==========================
# MAIN
# ==========================
def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN vacío. Configura TELEGRAM_TOKEN en variables de entorno.")

    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hora", hora))
    app.add_handler(CommandHandler("reboot", reboot))
    app.add_handler(CommandHandler("showp", showp))
    app.add_handler(CommandHandler("showd", showd))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("listp", list_photos_command))
    app.add_handler(CommandHandler("listd", list_documents_command))
    app.add_handler(CommandHandler("show", show_command))
    app.add_handler(CommandHandler("gop", go_photos_command))
    app.add_handler(CommandHandler("god", go_documents_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("rmp", rmp_command))
    app.add_handler(CommandHandler("rmd", rmd_command))
    app.add_handler(CommandHandler("mvp", mvp_command))
    app.add_handler(CommandHandler("mvd", mvd_command))
    app.add_handler(CommandHandler("mkdirp", mkdir_photos_command))
    app.add_handler(CommandHandler("mkdird", mkdir_documents_command))
    app.add_handler(CommandHandler("rnp", rename_photos_command))
    app.add_handler(CommandHandler("rnd", rename_documents_command))

    app.add_handler(MessageHandler(filters.PHOTO, save_img))
    app.add_handler(MessageHandler(filters.Document.ALL, save_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_reply))
    app.add_handler(CallbackQueryHandler(operations_callback, pattern=r"^OPS\|"))
    app.add_handler(CallbackQueryHandler(file_browser_callback, pattern=r"^FB\|"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Bot de Telegram iniciado...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
