from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


class FileStorageError(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    file_hash: str
    safe_file_name: str
    size_bytes: int
    storage_key: str
    absolute_path: Path
    created_new: bool


class IntakeFileStore:
    """Content-addressed file storage with no user-controlled path segments."""

    def __init__(self, root: str | Path, *, max_bytes: int = 25 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    def store(self, content: bytes, filename: str) -> StoredFile:
        if len(content) > self.max_bytes:
            raise FileStorageError("文件超过允许大小")
        safe_name = safe_filename(filename)
        digest = hashlib.sha256(content).hexdigest()
        # 物理对象只由内容哈希决定。原文件名和类型保存在数据库审计记录中，
        # 不能让同一内容仅通过更换后缀制造多个无台账副本。
        storage_key = f"sha256/{digest[:2]}/{digest}"
        target = (self.root / storage_key).resolve()
        if self.root not in target.parents:
            raise FileStorageError("文件存储位置不安全")
        target.parent.mkdir(parents=True, exist_ok=True)
        created_new = False
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise FileStorageError("同名存储对象哈希不一致")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=target.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
                created_new = True
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return StoredFile(
            digest,
            safe_name,
            len(content),
            storage_key,
            target,
            created_new,
        )

    def read(self, storage_key: str) -> bytes:
        if not re.fullmatch(
            r"sha256/[0-9a-f]{2}/[0-9a-f]{64}(?:\.[a-z0-9]+)?",
            storage_key,
        ):
            raise FileStorageError("文件存储标识无效")
        target = (self.root / storage_key).resolve()
        if self.root not in target.parents or not target.is_file():
            raise FileStorageError("原始文件不存在")
        return target.read_bytes()


def safe_filename(filename: str) -> str:
    value = str(filename or "").strip()
    if (
        not value
        or "/" in value
        or "\\" in value
        or ":" in value
        or value in {".", ".."}
        or Path(value).name != value
    ):
        raise FileStorageError("文件名不安全")
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    if not value or len(value) > 240:
        raise FileStorageError("文件名为空或过长")
    return value
