from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree

from app.legal_ops_data_intake.calculator import CalculationBlocked, validate_rule_spec


class RulePackageError(ValueError):
    pass


@dataclass(frozen=True)
class RuleEvidence:
    heading: str
    line_start: int
    line_end: int
    excerpt: str


@dataclass(frozen=True)
class RuleReferenceDocument:
    path: str
    file_hash: str
    paragraphs: tuple[str, ...]

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)


@dataclass(frozen=True)
class InspectedRulePackage:
    package_hash: str
    skill_path: str
    skill_name: str
    skill_version: str
    skill_markdown: str
    references: tuple[str, ...]
    evidence: tuple[RuleEvidence, ...]
    rule_spec: dict[str, Any] | None
    activation_ready: bool
    blocking_reasons: tuple[str, ...]
    reference_documents: tuple[RuleReferenceDocument, ...] = ()
    has_executable_files: bool = False


_ALLOWED_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".xlsx",
    ".docx",
}
_MAX_PACKAGE_BYTES = 20 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_ENTRIES = 500
_MAX_WORKBOOK_ENTRIES = 2_000
_MAX_WORKBOOK_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_MAX_DOCX_ENTRIES = 2_000
_MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_REFERENCE_PARAGRAPHS = 20_000
_MAX_REFERENCE_CHARACTERS = 500_000
_WORDPROCESSINGML_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)


def inspect_rule_package(content: bytes, filename: str) -> InspectedRulePackage:
    if len(content) > _MAX_PACKAGE_BYTES:
        raise RulePackageError("绩效技能包超过 20MB 限制")
    if filename.lower() == "skill.md":
        return _inspect_single_skill_markdown(content)
    if not filename.lower().endswith(".zip"):
        raise RulePackageError("请上传 Workbuddy 技能包 zip，或直接上传单个 SKILL.md")
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise RulePackageError("技能包损坏或文件类型不正确")

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if not entries or len(entries) > _MAX_ENTRIES:
            raise RulePackageError("技能包文件数量为空或超过限制")
        if sum(item.file_size for item in entries) > _MAX_UNCOMPRESSED_BYTES:
            raise RulePackageError("技能包解压后的总大小超过 50MB 限制")
        normalized_names: list[str] = []
        reference_documents: list[RuleReferenceDocument] = []
        for item in entries:
            normalized = item.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.name
                or ":" in path.parts[0]
                or normalized.startswith("/")
            ):
                raise RulePackageError("技能包包含不安全的文件路径")
            mode = item.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise RulePackageError("技能包不能包含符号链接")
            suffix = path.suffix.lower()
            if suffix not in _ALLOWED_EXTENSIONS:
                raise RulePackageError(
                    f"技能包包含不允许的文件类型：{suffix or '无扩展名'}"
                )
            if suffix == ".xlsm":
                raise RulePackageError("不支持带宏的 Excel 文件")
            if item.flag_bits & 0x1:
                raise RulePackageError("技能包不能包含加密文件")
            if suffix == ".xlsx":
                nested = archive.read(item)
                if not zipfile.is_zipfile(io.BytesIO(nested)):
                    raise RulePackageError("技能包内的 xlsx 模板损坏或扩展名伪造")
                with zipfile.ZipFile(io.BytesIO(nested)) as workbook:
                    _validate_nested_workbook(workbook)
            if suffix == ".docx":
                nested = archive.read(item)
                if not zipfile.is_zipfile(io.BytesIO(nested)):
                    raise RulePackageError("技能包内的 docx 参考文档损坏或扩展名伪造")
                with zipfile.ZipFile(io.BytesIO(nested)) as document:
                    paragraphs = _read_docx_paragraphs(document)
                reference_documents.append(
                    RuleReferenceDocument(
                        path=normalized,
                        file_hash=hashlib.sha256(nested).hexdigest(),
                        paragraphs=paragraphs,
                    )
                )
            normalized_names.append(normalized)

        skill_names = [
            name
            for name in normalized_names
            if PurePosixPath(name).name.lower() == "skill.md"
        ]
        if len(skill_names) != 1:
            raise RulePackageError("技能包必须且只能包含一个 SKILL.md")
        skill_path = skill_names[0]
        try:
            skill_markdown = archive.read(skill_path).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RulePackageError("SKILL.md 必须使用 UTF-8 编码") from None
        if not skill_markdown.strip():
            raise RulePackageError("SKILL.md 不能为空")
        metadata = _frontmatter(skill_markdown)
        skill_name = str(
            metadata.get("name")
            or metadata.get("title")
            or PurePosixPath(skill_path).parent.name
            or "未命名技能"
        )
        skill_version = str(
            metadata.get("version") or metadata.get("rule_version") or "未声明"
        )
        if len(skill_path) > 512 or len(skill_name) > 256 or len(skill_version) > 100:
            raise RulePackageError("技能名称、版本或 SKILL.md 路径超过长度限制")
        evidence = _extract_evidence(skill_markdown)
        references = tuple(
            sorted(name for name in normalized_names if name != skill_path)
        )

        candidates = [
            name
            for name in normalized_names
            if PurePosixPath(name).name.lower() == "rule-spec.json"
        ]
        rule_spec: dict[str, Any] | None = None
        blocking: list[str] = []
        if len(candidates) > 1:
            raise RulePackageError("技能包包含多个 rule-spec.json，无法确定规则版本")
        if candidates:
            try:
                parsed = json.loads(archive.read(candidates[0]).decode("utf-8-sig"))
                rule_spec = validate_rule_spec(parsed)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                CalculationBlocked,
            ) as exc:
                raise RulePackageError(f"rule-spec.json 校验失败：{exc}") from exc
            blocking.append("规则结构已校验，仍需数据发布管理员确认后方可启用")
        else:
            blocking.append("未找到经校验的结构化规则文件 rule-spec.json")

    return InspectedRulePackage(
        package_hash=hashlib.sha256(content).hexdigest(),
        skill_path=skill_path,
        skill_name=skill_name,
        skill_version=skill_version,
        skill_markdown=skill_markdown,
        references=references,
        evidence=evidence,
        rule_spec=rule_spec,
        activation_ready=False,
        blocking_reasons=tuple(blocking),
        reference_documents=tuple(reference_documents),
    )


def _inspect_single_skill_markdown(content: bytes) -> InspectedRulePackage:
    try:
        skill_markdown = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RulePackageError("SKILL.md 必须使用 UTF-8 编码") from None
    if not skill_markdown.strip():
        raise RulePackageError("SKILL.md 不能为空")
    metadata = _frontmatter(skill_markdown)
    skill_name = str(metadata.get("name") or metadata.get("title") or "未命名技能")
    skill_version = str(
        metadata.get("version") or metadata.get("rule_version") or "未声明"
    )
    if len(skill_name) > 256 or len(skill_version) > 100:
        raise RulePackageError("技能名称或版本超过长度限制")
    return InspectedRulePackage(
        package_hash=hashlib.sha256(content).hexdigest(),
        skill_path="SKILL.md",
        skill_name=skill_name,
        skill_version=skill_version,
        skill_markdown=skill_markdown,
        references=(),
        evidence=_extract_evidence(skill_markdown),
        rule_spec=None,
        activation_ready=False,
        blocking_reasons=("未找到经校验的结构化规则文件 rule-spec.json",),
    )


def _validate_nested_workbook(workbook: zipfile.ZipFile) -> None:
    entries = workbook.infolist()
    if len(entries) > _MAX_WORKBOOK_ENTRIES:
        raise RulePackageError("技能包内的 Excel 模板文件数量超过安全限制")
    if sum(entry.file_size for entry in entries) > _MAX_WORKBOOK_UNCOMPRESSED_BYTES:
        raise RulePackageError("技能包内的 Excel 模板解压后超过安全限制")
    if any(entry.flag_bits & 0x1 for entry in entries):
        raise RulePackageError("技能包内不能包含加密的 Excel 模板")
    if any(entry.filename.lower().endswith("vbaproject.bin") for entry in entries):
        raise RulePackageError("技能包内的 Excel 模板包含宏，已拒绝")


def _read_docx_paragraphs(document: zipfile.ZipFile) -> tuple[str, ...]:
    entries = document.infolist()
    if len(entries) > _MAX_DOCX_ENTRIES:
        raise RulePackageError("技能包内的 Word 参考文档文件数量超过安全限制")
    if sum(entry.file_size for entry in entries) > _MAX_DOCX_UNCOMPRESSED_BYTES:
        raise RulePackageError("技能包内的 Word 参考文档解压后超过安全限制")
    if any(entry.flag_bits & 0x1 for entry in entries):
        raise RulePackageError("技能包内不能包含加密的 Word 参考文档")
    lowered = {entry.filename.replace("\\", "/").lower() for entry in entries}
    if "[content_types].xml" not in lowered or "word/document.xml" not in lowered:
        raise RulePackageError("技能包内的 docx 参考文档结构不完整")
    if any(
        name.endswith("vbaproject.bin")
        or name.startswith(("word/activex/", "word/embeddings/"))
        for name in lowered
    ):
        raise RulePackageError("技能包内的 Word 参考文档包含宏或嵌入对象，已拒绝")

    raw_xml = document.read("word/document.xml")
    upper_xml = raw_xml.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise RulePackageError("技能包内的 Word 参考文档包含不安全的 XML")
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError:
        raise RulePackageError("技能包内的 Word 参考文档正文损坏") from None

    paragraph_tag = f"{{{_WORDPROCESSINGML_NAMESPACE}}}p"
    text_tag = f"{{{_WORDPROCESSINGML_NAMESPACE}}}t"
    tab_tag = f"{{{_WORDPROCESSINGML_NAMESPACE}}}tab"
    break_tag = f"{{{_WORDPROCESSINGML_NAMESPACE}}}br"
    paragraphs: list[str] = []
    character_count = 0
    for paragraph in root.iter(paragraph_tag):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == text_tag and node.text:
                pieces.append(node.text)
            elif node.tag == tab_tag:
                pieces.append("\t")
            elif node.tag == break_tag:
                pieces.append("\n")
        text_value = "".join(pieces).strip()
        if not text_value:
            continue
        paragraphs.append(text_value)
        character_count += len(text_value)
        if (
            len(paragraphs) > _MAX_REFERENCE_PARAGRAPHS
            or character_count > _MAX_REFERENCE_CHARACTERS
        ):
            raise RulePackageError("Word 参考文档文字内容超过安全限制")
    if not paragraphs:
        raise RulePackageError("Word 参考文档没有可读取的正文")
    return tuple(paragraphs)


def _frontmatter(markdown: str) -> dict[str, str]:
    lines = markdown.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if re.fullmatch(r"[A-Za-z0-9_-]+", key.strip()):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _extract_evidence(markdown: str) -> tuple[RuleEvidence, ...]:
    lines = markdown.splitlines()
    headings: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            headings.append((index, match.group(1).strip()))
    evidence: list[RuleEvidence] = []
    for position, (line_start, heading) in enumerate(headings):
        line_end = (
            headings[position + 1][0] - 1
            if position + 1 < len(headings)
            else len(lines)
        )
        excerpt = "\n".join(
            lines[line_start - 1 : min(line_end, line_start + 7)]
        ).strip()
        evidence.append(RuleEvidence(heading, line_start, line_end, excerpt))
    return tuple(evidence)
