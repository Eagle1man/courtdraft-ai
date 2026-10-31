"""Хранилище дел: оригиналы файлов на диске.

Зачем отдельно от базы: в базе лежит извлечённый текст, а суду нужен
исходный документ — с подписями, печатями, разметкой. Без оригинала
нельзя предъявить, что программа ничего не исказила при извлечении.

Раскладка:

    <storage_dir>/originals/<case_id>/<sha256[:12]>_<имя файла>

Дело — отдельный каталог, поэтому:
  * видно, сколько занимает одно дело;
  * удаление дела убирает его файлы одним движением;
  * поиск нужного файла не требует запроса к базе.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from app.config import Settings


class StorageError(ValueError):
    """Ошибка хранилища. Текст показывается человеку."""


def originals_root(cfg: Settings) -> Path:
    """Корень оригиналов. Создаётся при первом обращении."""
    root = Path(cfg.storage_dir) / "originals"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(filename: str) -> str:
    """Имя файла без пути и служебных символов.

    `../../.env` в качестве имени не должен никуда выйти за пределы
    каталога дела: имя приходит от клиента и доверять ему нельзя.
    """
    name = Path(filename or "документ").name
    name = re.sub(r"[^\w.\-() ]+", "_", name, flags=re.UNICODE).strip(" ._")
    return name[:180] or "документ"


def case_dir(cfg: Settings, case_id: uuid.UUID | str) -> Path:
    path = originals_root(cfg) / str(case_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_original(
    cfg: Settings,
    case_id: uuid.UUID | str,
    sha256: str,
    filename: str,
    data: bytes,
) -> str:
    """Сохранить оригинал. Возвращает путь относительно корня оригиналов.

    Имя начинается с хэша: два файла с одинаковым именем в одном деле
    не затрут друг друга, а содержимое остаётся проверяемым по SHA256.
    """
    if not data:
        raise StorageError("Файл пуст — сохранять нечего")

    folder = case_dir(cfg, case_id)
    target = folder / f"{sha256[:12]}_{_safe_name(filename)}"

    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)

    return target.relative_to(originals_root(cfg)).as_posix()


def resolve(cfg: Settings, relative: str) -> Path | None:
    """Найти файл по относительному пути. None — если его нет.

    Проверка на выход за корень обязательна: путь приходит из базы,
    но базу могли наполнить раньше небезопасным кодом.
    """
    if not relative:
        return None
    root = originals_root(cfg).resolve()
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root)):
        return None
    return candidate if candidate.is_file() else None


def delete_case_files(cfg: Settings, case_id: uuid.UUID | str) -> bool:
    """Удалить все оригиналы дела."""
    path = originals_root(cfg) / str(case_id)
    if not path.is_dir():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def case_size_bytes(cfg: Settings, case_id: uuid.UUID | str) -> int:
    path = originals_root(cfg) / str(case_id)
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def storage_summary(cfg: Settings) -> dict[str, int]:
    """Сколько занимает хранилище: всего и по делам.

    Показывается человеку, чтобы он видел, когда заканчивается место,
    а не узнавал об этом в момент загрузки.
    """
    root = originals_root(cfg)
    cases = 0
    total = 0
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        cases += 1
        total += sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())

    for candidate in (root, Path(cfg.storage_dir)):
        if candidate.is_dir():
            break

    free = shutil.disk_usage(Path(cfg.storage_dir)).free
    return {
        "cases": cases,
        "bytes": total,
        "free_bytes": free,
    }
