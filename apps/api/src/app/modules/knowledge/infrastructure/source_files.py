"""Файлы источника в каталоге `<root>/<tenant_id>/<source_id>/` (ADR-0012).

Общая часть коннекторов `file` и `table`: каталог задаёт платформа, а не конфиг источника, —
поэтому файлы другого тенанта недостижимы. Симлинки и скрытые файлы не читаются.
`external_id` файла — относительный путь (posix), курсор — максимальный `mtime_ns`.
"""

import os
import shutil
import uuid
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from app.modules.knowledge.domain.ingestion import SourceSpec
from app.modules.knowledge.domain.source_admin import SourceFileInfo
from app.modules.knowledge.domain.source_seed import MirrorStats


class SourceFileError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SourceFile:
    external_id: str
    path: Path
    mtime_ns: int
    size: int


def source_dir(root: Path, source: SourceSpec) -> Path:
    return root / str(source.tenant_id) / str(source.source_id)


class SourceFiles:
    """Каталоги источников под `root`; читаются только файлы с расширениями `suffixes`."""

    def __init__(self, root: Path, suffixes: Collection[str]) -> None:
        self.root = root
        self.suffixes = frozenset(s.lower() for s in suffixes)

    def source_dir(self, source: SourceSpec) -> Path:
        return source_dir(self.root, source)

    def scan(self, source: SourceSpec) -> list[SourceFile]:
        base = self.source_dir(source)
        # Нет каталога — ошибка, а не пустой источник: иначе полный discover удалит всё.
        if not base.is_dir():
            raise SourceFileError(f"каталог источника не найден: {base}")
        files: list[SourceFile] = []
        for dirpath, dirnames, filenames in os.walk(base):  # симлинки на каталоги не обходит
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath, name)
                if name.startswith(".") or path.is_symlink() or not self.supported(path):
                    continue
                stat = path.stat()
                external_id = path.relative_to(base).as_posix()
                files.append(SourceFile(external_id, path, stat.st_mtime_ns, stat.st_size))
        return files

    def resolve(self, source: SourceSpec, external_id: str) -> Path:
        """Путь файла источника; `..`, абсолютные пути и симлинки отклоняются."""
        base = self.source_dir(source)
        relative = PurePosixPath(external_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise SourceFileError(f"недопустимый путь: {external_id}")
        path = base.joinpath(*relative.parts)
        inside = path.resolve().is_relative_to(base.resolve())  # симлинк в пути к файлу
        if not inside or path.is_symlink() or not path.is_file():
            raise SourceFileError(f"файл не найден: {external_id}")
        if not self.supported(path):
            raise SourceFileError(f"неподдерживаемый формат: {external_id}")
        return path

    def supported(self, path: Path) -> bool:
        return path.suffix.lower() in self.suffixes


def files_cursor(files: Collection[SourceFile], fallback: str | None = None) -> str | None:
    return str(max(f.mtime_ns for f in files)) if files else fallback


class LocalSourceFileStore:
    """`SourceFileStore` на локальном диске: seed файлов источника из YAML тенанта (ADR-0019)
    и файлы из админки (ADR-0037)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def mirror(self, source: SourceSpec, from_dir: Path) -> MirrorStats:
        target = source_dir(self.root, source)
        target.mkdir(parents=True, exist_ok=True)
        wanted = dict(_visible_files(from_dir))
        copied = unchanged = 0
        for relative, path in wanted.items():
            destination = target / relative
            if destination.is_file() and destination.read_bytes() == path.read_bytes():
                unchanged += 1
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            # copyfile, не copy2: новый mtime — изменение увидит и инкрементальная синхронизация.
            shutil.copyfile(path, destination)
            copied += 1
        removed = 0
        for relative, path in list(_visible_files(target)):
            if relative not in wanted:
                path.unlink()
                removed += 1
        return MirrorStats(copied=copied, removed=removed, unchanged=unchanged)

    def list_files(self, source: SourceSpec) -> list[SourceFileInfo]:
        base = source_dir(self.root, source)
        if not base.is_dir():
            return []
        result: list[SourceFileInfo] = []
        for relative, path in _visible_files(base):
            stat = path.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime_ns / 1e9, tz=UTC)
            result.append(SourceFileInfo(relative, stat.st_size, modified_at))
        return sorted(result, key=lambda f: f.name)

    def put_file(self, source: SourceSpec, name: str, data: bytes) -> SourceFileInfo:
        target = source_dir(self.root, source)
        destination = target / _plain_name(name)
        target.mkdir(parents=True, exist_ok=True)
        # Скрытый временный файл scan не читает; rename атомарен — синхронизация не увидит
        # недописанный файл.
        temporary = target / f".upload-{uuid.uuid4().hex}"
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        stat = destination.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime_ns / 1e9, tz=UTC)
        return SourceFileInfo(name, stat.st_size, modified_at)

    def delete_file(self, source: SourceSpec, name: str) -> bool:
        path = source_dir(self.root, source) / _plain_name(name)
        if path.is_symlink() or not path.is_file():
            return False
        path.unlink()
        return True


def _plain_name(name: str) -> str:
    """Имя файла в корне каталога источника: без путей и скрытых файлов."""
    if not name or name != PurePosixPath(name).name or "\\" in name or name.startswith("."):
        raise SourceFileError(f"недопустимое имя файла: {name!r}")
    return name


def _visible_files(base: Path) -> Iterator[tuple[str, Path]]:
    """(относительный posix-путь, путь) файлов без скрытых и симлинков — как читает `scan`."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if not name.startswith(".") and not path.is_symlink():
                yield path.relative_to(base).as_posix(), path
