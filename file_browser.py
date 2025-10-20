from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Optional, Sequence, Set

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from helpers import chunk_numbered_lines, ensure_within_base


SendEntryCallback = Callable[[Update, Path], Awaitable[None]]


@dataclass
class Entry:
    name: str
    path: Path
    is_dir: bool
    file_emoji: str

    @property
    def display_name(self) -> str:
        suffix = "/" if self.is_dir else ""
        return f"{self.name}{suffix}"

    @property
    def emoji(self) -> str:
        return "📂" if self.is_dir else self.file_emoji


class FileBrowser:
    """Navegador de archivos parametrizable por tipo (fotos, documentos, etc.)."""

    ACTIVE_KEY = "file_browser:active"

    def __init__(
        self,
        *,
        namespace: str,
        base_dir: Path,
        send_entry: SendEntryCallback,
        file_emoji: str,
        item_label_singular: str,
        item_label_plural: Optional[str] = None,
        item_article: Optional[str] = None,
        allowed_extensions: Optional[Iterable[str]] = None,
        selection_prompt: Optional[str] = None,
        show_command: str = "show",
        allow_text_commands: bool = True,
    ):
        self.namespace = namespace
        self.base_dir = base_dir.resolve()
        self._send_entry = send_entry
        self.file_emoji = file_emoji
        self.item_label_singular = item_label_singular
        self.item_label_plural = item_label_plural or f"{item_label_singular}s"
        self.item_article = item_article or self._infer_article(item_label_singular)
        self.show_command = show_command.lstrip("/") or "show"
        self.selection_prompt = (
            selection_prompt
            or f"Elige {self._article()} {self.item_label_singular} tocando un botón "
            f"o responde con /{self.show_command} <número> (o solo el número)."
        )
        self.allow_text_commands = allow_text_commands
        self.allowed_extensions: Optional[Set[str]] = (
            {
                ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                for ext in allowed_extensions
            }
            if allowed_extensions
            else None
        )

        self.listing_key = f"{self.namespace}:listing"
        self.matches_key = f"{self.namespace}:matches"
        self.path_key = f"{self.namespace}:path"
        self.callback_prefix = f"FB|{self.namespace}|"

    # ---------------------
    # Estado
    # ---------------------
    def _current_path(self, context: ContextTypes.DEFAULT_TYPE) -> Path:
        stored = context.user_data.get(self.path_key)
        if stored:
            try:
                candidate = ensure_within_base(self.base_dir, Path(stored))
                if candidate.exists():
                    return candidate
            except ValueError:
                pass
        context.user_data[self.path_key] = str(self.base_dir)
        return self.base_dir

    def _set_path(self, context: ContextTypes.DEFAULT_TYPE, new_path: Path) -> None:
        safe_path = ensure_within_base(self.base_dir, new_path)
        context.user_data[self.path_key] = str(safe_path)
        self._set_active(context)

    def get_current_path(self, context: ContextTypes.DEFAULT_TYPE) -> Path:
        return self._current_path(context)

    def _set_active(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data[self.ACTIVE_KEY] = self.namespace

    def _store_listing(self, context: ContextTypes.DEFAULT_TYPE, entries: Sequence[Entry]) -> None:
        context.user_data[self.listing_key] = [
            {
                "relative": str(entry.path.relative_to(self.base_dir)),
                "is_dir": entry.is_dir,
            }
            for entry in entries
        ]
        context.user_data.pop(self.matches_key, None)
        self._set_active(context)

    def _store_matches(self, context: ContextTypes.DEFAULT_TYPE, matches: Sequence[Path]) -> None:
        context.user_data[self.matches_key] = [str(path.relative_to(self.base_dir)) for path in matches]
        self._set_active(context)

    def _pop_matches(self, context: ContextTypes.DEFAULT_TYPE) -> List[str]:
        matches = context.user_data.pop(self.matches_key, [])
        return matches

    # ---------------------
    # Utilidades
    # ---------------------
    def _entries_for_path(self, path: Path) -> List[Entry]:
        if not path.exists():
            return []

        dirs: List[Entry] = []
        files: List[Entry] = []

        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir():
                dirs.append(Entry(name=child.name, path=child, is_dir=True, file_emoji=self.file_emoji))
            elif self._is_valid_file(child):
                files.append(Entry(name=child.name, path=child, is_dir=False, file_emoji=self.file_emoji))

        return dirs + files

    def _is_valid_file(self, path: Path) -> bool:
        if not path.is_file():
            return False
        if self.allowed_extensions is None:
            return True
        return path.suffix.lower() in self.allowed_extensions

    def _listing_messages(self, current_path: Path, entries: Sequence[Entry]) -> List[str]:
        header = f"📂 {current_path}/"
        lines = [f"{entry.emoji} {entry.display_name}" for entry in entries]
        messages = chunk_numbered_lines(header, lines)
        return messages if messages else [header + "\n(vacío)"]

    # ---------------------
    # Listado
    # ---------------------
    async def handle_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return

        current_path = self._current_path(context)
        entries = self._entries_for_path(current_path)
        self._store_listing(context, entries)

        for block in self._listing_messages(current_path, entries):
            await message.reply_text(block)

    # ---------------------
    # Mostrar archivo
    # ---------------------
    async def handle_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
        message = update.effective_message
        if not message:
            return

        query = (query or "").strip()
        if not query:
            article = self._article()
            await message.reply_text(
                f"⚠️ Debes indicar parte del nombre de {article} {self.item_label_singular}. "
                f"Ejemplo: /{self.show_command} lago"
            )
            return

        if query.isdigit():
            processed = await self.handle_number_selection(update, context, int(query))
            if processed:
                return

        current_path = self._current_path(context)
        matches = [
            child
            for child in sorted(current_path.iterdir(), key=lambda p: p.name.lower())
            if self._is_valid_file(child) and query.lower() in child.name.lower()
        ]

        if not matches:
            await message.reply_text(
                f"❌ No encontré {self.item_label_plural} con ese nombre en este directorio."
            )
            return

        if len(matches) == 1:
            await self._send_entry(update, matches[0])
            self._set_active(context)
            return

        self._store_matches(context, matches)
        lines = [f"{self.file_emoji} {path.name}" for path in matches]
        header = f"🔍 Existen {len(matches)} coincidencias:"
        messages = chunk_numbered_lines(header, lines)
        for block in messages:
            await message.reply_text(block)

        keyboard = self._build_keyboard_for_matches(matches)
        await message.reply_text(self.selection_prompt, reply_markup=keyboard)

    def _build_keyboard_for_matches(self, matches: Sequence[Path]) -> InlineKeyboardMarkup:
        buttons: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []
        for idx, match in enumerate(matches, 1):
            relative = match.relative_to(self.base_dir)
            callback_data = f"{self.callback_prefix}{relative}"
            row.append(InlineKeyboardButton(text=f"{idx}", callback_data=callback_data))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return InlineKeyboardMarkup(buttons)

    # ---------------------
    # Navegación
    # ---------------------
    async def handle_go(self, update: Update, context: ContextTypes.DEFAULT_TYPE, target: str) -> None:
        message = update.effective_message
        if not message:
            return

        target = target.strip()
        if not target:
            await message.reply_text(f"⚠️ Usa: go <directorio> o go..")
            return

        if target in {"..", "../", "go.."}:
            await self._go_up(update, context)
            return

        current_path = self._current_path(context)
        selected_dir = self._find_directory(current_path, target)
        if not selected_dir:
            await message.reply_text(f"❌ No encontré el directorio '{target}'.")
            return

        self._set_path(context, selected_dir)
        await self.handle_list(update, context)

    async def _go_up(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return

        current_path = self._current_path(context)
        if current_path == self.base_dir:
            await message.reply_text("🔝 Ya estás en el directorio raíz.")
            await self.handle_list(update, context)
            return

        parent = ensure_within_base(self.base_dir, current_path.parent)
        self._set_path(context, parent)
        await self.handle_list(update, context)

    def _find_directory(self, current_path: Path, target: str) -> Optional[Path]:
        stripped = target.rstrip("/")

        for child in current_path.iterdir():
            if child.is_dir() and child.name == stripped:
                return child

        normalized = stripped.lower()
        for child in sorted(current_path.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and child.name.lower() == normalized:
                return child

        return None

    # ---------------------
    # Selecciones numéricas
    # ---------------------
    async def handle_number_selection(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        number: int,
    ) -> bool:
        message = update.effective_message
        if not message:
            return False

        matches = context.user_data.get(self.matches_key, [])
        if matches:
            if 1 <= number <= len(matches):
                relative = matches[number - 1]
                await self._send_entry(update, self._resolve_relative(relative))
                context.user_data.pop(self.matches_key, None)
                self._set_active(context)
            else:
                await message.reply_text("⚠️ Número fuera de rango.")
            return True

        listing = context.user_data.get(self.listing_key)
        if not listing:
            return False

        if not (1 <= number <= len(listing)):
            await message.reply_text("⚠️ Número fuera de rango.")
            return True

        entry = listing[number - 1]
        path = self._resolve_relative(entry["relative"])
        if entry["is_dir"]:
            self._set_path(context, path)
            await self.handle_list(update, context)
        else:
            await self._send_entry(update, path)
        return True

    def _resolve_relative(self, relative: str) -> Path:
        return ensure_within_base(self.base_dir, self.base_dir / Path(relative))

    # ---------------------
    # Texto libre y callbacks
    # ---------------------
    async def process_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False

        lowered = stripped.lower()
        active_namespace = context.user_data.get(self.ACTIVE_KEY)
        has_context = bool(context.user_data.get(self.listing_key) or context.user_data.get(self.matches_key))

        if stripped.isdigit():
            if active_namespace != self.namespace and not (self.allow_text_commands and has_context):
                return False
            return await self.handle_number_selection(update, context, int(stripped))

        if lowered in {"go..", "go .."} and active_namespace == self.namespace:
            await self._go_up(update, context)
            return True

        if lowered.startswith("go ") and active_namespace == self.namespace:
            await self.handle_go(update, context, stripped[3:])
            return True

        trigger = f"{self.show_command} "
        slash_trigger = f"/{self.show_command} "

        if lowered.startswith(trigger):
            if active_namespace != self.namespace and not self.allow_text_commands:
                return False
            await self.handle_show(update, context, stripped[len(trigger):])
            return True

        if lowered.startswith(slash_trigger):
            if active_namespace != self.namespace and not self.allow_text_commands:
                return False
            await self.handle_show(update, context, stripped[len(slash_trigger):])
            return True

        if not self.allow_text_commands:
            return False

        if lowered in {"list", "/list"}:
            await self.handle_list(update, context)
            return True

        return False

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = update.callback_query
        if not query or not query.data:
            return False

        if not query.data.startswith(self.callback_prefix):
            return False

        await query.answer()
        relative = query.data.split("|", 2)[-1]
        try:
            path = self._resolve_relative(relative)
        except ValueError:
            await query.edit_message_text("❌ Ruta inválida.")
            return True

        await self._send_entry(update, path)
        self._set_active(context)
        return True

    def _article(self) -> str:
        return self.item_article

    @staticmethod
    def _infer_article(word: str) -> str:
        lowered = word.lower()
        if lowered.endswith(("a", "ión", "dad", "tud")):
            return "la"
        return "el"


# Retrocompatibilidad
PhotoBrowser = FileBrowser
