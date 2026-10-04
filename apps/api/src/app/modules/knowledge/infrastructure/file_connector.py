"""Коннектор `file`: файлы источника из каталога `<root>/<tenant_id>/<source_id>/` (ADR-0012).

Каталог, симлинки, скрытые файлы и курсор — см. `source_files`. Каждый файл — один
документ, приведённый к markdown (`file_parsers`).
"""

import asyncio
from collections.abc import Mapping
from pathlib import Path

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
from app.modules.knowledge.infrastructure.source_files import (
    SourceFile,
    SourceFileError,
    SourceFiles,
    files_cursor,
)

DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024


class FileConnector:
    kind = SourceKind.FILE

    def __init__(
        self,
        root: Path,
        *,
        parsers: Mapping[str, FileParser] = PARSERS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.files = SourceFiles(root, parsers.keys())
        self.parsers = parsers
        self.max_file_bytes = max_file_bytes

    def source_dir(self, source: SourceSpec) -> Path:
        return self.files.source_dir(source)

    async def discover(self, source: SourceSpec) -> Listing:
        files = await asyncio.to_thread(self.files.scan, source)
        return _listing(files)

    async def changed_since(self, source: SourceSpec, cursor: str) -> Listing:
        since = int(cursor)
        files = await asyncio.to_thread(self.files.scan, source)
        return _listing([f for f in files if f.mtime_ns > since], fallback=cursor)

    async def fetch(self, source: SourceSpec, ref: RawItemRef) -> RawItem:
        return await asyncio.to_thread(self._read, source, ref.external_id)

    def _read(self, source: SourceSpec, external_id: str) -> DocumentItem:
        path = self.files.resolve(source, external_id)
        parser = self.parsers[path.suffix.lower()]
        if path.stat().st_size > self.max_file_bytes:
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


def _listing(files: list[SourceFile], fallback: str | None = None) -> Listing:
    refs = [RawItemRef(f.external_id, str(f.mtime_ns)) for f in files]
    return Listing(refs, files_cursor(files, fallback))
