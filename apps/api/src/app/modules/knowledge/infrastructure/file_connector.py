"""Коннектор `file`: файлы источника из каталога `<root>/<tenant_id>/<source_id>/` (ADR-0012).

Каталог задаёт платформа, а не конфиг источника, — поэтому файлы другого тенанта
недостижимы. Симлинки и скрытые файлы не читаются. `external_id` — относительный путь
(posix), курсор — максимальный `mtime_ns` среди файлов.
"""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.modules.knowledge.domain.entities import SourceKind
from app.modules.knowledge.domain.ingestion import (
    DocumentItem,
    Listing,
    RawItem,
    RawItemRef,
    SourceSpec,
)
from app.modules.knowledge.infrastructure.file_parsers import (
    PARSERS,
    FileParseError,
    FileParser,
)

DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024


class SourceFileError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _FileInfo:
    external_id: str
    mtime_ns: int


class FileConnector:
    kind = SourceKind.FILE

    def __init__(
        self,
        root: Path,
        *,
        parsers: Mapping[str, FileParser] = PARSERS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.root = root
        self.parsers = parsers
        self.max_file_bytes = max_file_bytes

    def source_dir(self, source: SourceSpec) -> Path:
        return self.root / str(source.tenant_id) / str(source.source_id)

    async def discover(self, source: SourceSpec) -> Listing:
        files = await asyncio.to_thread(self._scan, self.source_dir(source))
        return _listing(files)

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        since = int(cursor)
        files = await asyncio.to_thread(self._scan, self.source_dir(source))
        return _listing([f for f in files if f.mtime_ns > since], fallback=cursor)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        return await asyncio.to_thread(self._read, self.source_dir(source), ref.external_id)

    def _scan(self, base: Path) -> list[_FileInfo]:
        # Нет каталога — ошибка, а не пустой источник: иначе полный discover удалит всё.
        if not base.is_dir():
            raise SourceFileError(f"каталог источника не найден: {base}")
        files: list[_FileInfo] = []
        for dirpath, dirnames, filenames in os.walk(base):  # симлинки на каталоги не обходит
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath, name)
                if name.startswith(".") or path.is_symlink() or not self._supported(path):
                    continue
                external_id = path.relative_to(base).as_posix()
                files.append(_FileInfo(external_id, path.stat().st_mtime_ns))
        return files

    def _read(self, base: Path, external_id: str) -> DocumentItem:
        relative = PurePosixPath(external_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise SourceFileError(f"недопустимый путь: {external_id}")
        path = base.joinpath(*relative.parts)
        inside = path.resolve().is_relative_to(base.resolve())  # симлинк в пути к файлу
        if not inside or path.is_symlink() or not path.is_file():
            raise SourceFileError(f"файл не найден: {external_id}")
        parser = self.parsers.get(path.suffix.lower())
        if parser is None:
            raise SourceFileError(f"неподдерживаемый формат: {external_id}")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise SourceFileError(f"файл больше {self.max_file_bytes} байт: {external_id}")
        try:
            parsed = parser(path.read_bytes())
        except FileParseError as e:
            raise SourceFileError(f"{external_id}: {e}") from e
        return DocumentItem(
            external_id=external_id,
            title=parsed.title or path.stem,
            text=parsed.text,
            metadata={"path": external_id, "format": path.suffix.lower().lstrip(".")},
        )

    def _supported(self, path: Path) -> bool:
        return path.suffix.lower() in self.parsers


def _listing(files: list[_FileInfo], fallback: str | None = None) -> Listing:
    cursor = str(max(f.mtime_ns for f in files)) if files else fallback
    return Listing([RawItemRef(f.external_id, str(f.mtime_ns)) for f in files], cursor)
