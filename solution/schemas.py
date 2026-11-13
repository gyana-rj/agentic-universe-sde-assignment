"""Loading of per-workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_header

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"

FIELD_TYPES = {"string", "phone", "date", "decimal", "enum"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple[str, ...] = ()
    default: str | None = None
    aliases: tuple[str, ...] = ()

    def header_keys(self) -> set[str]:
        return {normalize_header(self.name), *(normalize_header(a) for a in self.aliases)}


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    fields: tuple[Field, ...]
    _by_header: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    def field_for_header(self, header: str) -> str | None:
        return self._by_header.get(normalize_header(header))


def load_schema(path: Path) -> WorkflowSchema:
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = []
    for f in raw["fields"]:
        if f["type"] not in FIELD_TYPES:
            raise ValueError(f"{path}: unknown field type {f['type']!r}")
        fields.append(Field(
            name=f["name"], type=f["type"], required=bool(f.get("required")),
            pii=bool(f.get("pii")), reference=bool(f.get("reference")),
            values=tuple(f.get("values", ())), default=f.get("default"),
            aliases=tuple(f.get("aliases", ())),
        ))
    by_header: dict[str, str] = {}
    for f in fields:
        for key in f.header_keys():
            if key in by_header and by_header[key] != f.name:
                raise ValueError(f"{path}: header {key!r} is ambiguous")
            by_header[key] = f.name
    schema = WorkflowSchema(raw["workflow_id"], tuple(fields), by_header)
    if sum(f.reference for f in fields) != 1:
        raise ValueError(f"{path}: exactly one field must be marked reference")
    if sum(f.type == "phone" for f in fields) != 1:
        raise ValueError(f"{path}: exactly one phone field is required (lead identity)")
    return schema


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    schemas = {}
    for p in sorted(schema_dir.glob("*.json")):
        s = load_schema(p)
        schemas[s.workflow_id] = s
    return schemas


@dataclass(frozen=True)
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def load_dispositions(schema_dir: Path) -> list[Disposition]:
    def as_bool(v: str) -> bool:
        v = v.strip().lower()
        if v not in {"true", "false"}:
            raise ValueError(f"dispositions.csv: bad boolean {v!r}")
        return v == "true"

    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8-sig") as f:
        return [Disposition(r["workflow_id"].strip(), r["code"].strip(), r["label"].strip(),
                            as_bool(r["is_terminal"]), as_bool(r["is_success"]))
                for r in csv.DictReader(f)]
