"""Lead CSV ingestion: header mapping, validation, rejection, merge."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .normalize import normalize_date, normalize_decimal, normalize_enum, normalize_phone
from .pii import PiiCodec
from .schemas import Field, WorkflowSchema


class BatchConflictError(RuntimeError):
    """A batch with this (workflow, batch_id) was already ingested with different content."""


# ---------------------------------------------------------------- pure logic

def _normalize(f: Field, value: str) -> str | None:
    if f.type == "string":
        return value
    if f.type == "phone":
        return normalize_phone(value)
    if f.type == "date":
        return normalize_date(value)
    if f.type == "decimal":
        return normalize_decimal(value)
    if f.type == "enum":
        return normalize_enum(value, list(f.values))
    raise AssertionError(f.type)


@dataclass
class RowResult:
    values: dict[str, str | None] | None   # None when rejected
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def validate_row(schema: WorkflowSchema, raw: dict[str, str]) -> RowResult:
    """Walk fields in schema order; the first hard failure is the reason.

    ``raw`` maps field name -> raw cell text (unmapped fields absent).
    Defaults are NOT applied here: they apply after merging.
    """
    values: dict[str, str | None] = {}
    warnings: list[str] = []
    for f in schema.fields:
        v = (raw.get(f.name) or "").strip()
        if not v:
            if f.required:
                return RowResult(None, f"missing_required:{f.name}")
            values[f.name] = None
            continue
        norm = _normalize(f, v)
        if norm is None:
            if f.type == "phone":
                return RowResult(None, "invalid_phone")
            if f.required:
                # Not reachable with the given schemas (required fields are
                # strings/phones); a required value we can't parse can't be used.
                return RowResult(None, f"invalid_value:{f.name}")
            warnings.append(f.name)
            values[f.name] = None
            continue
        values[f.name] = norm
    return RowResult(values, None, warnings)


def map_header(schema: WorkflowSchema, header: list[str]) -> list[tuple[int, str]]:
    """(column index, field name) for every column that matches a field."""
    return [(i, name) for i, h in enumerate(header)
            if (name := schema.field_for_header(h)) is not None]


def iter_data_rows(text: str, schema: WorkflowSchema):
    """Yield (row_number, {field: raw value}) for non-blank rows.

    If two columns map to the same field, the first non-empty one wins.
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:
        return
    mapping = map_header(schema, header)
    row_number = 0
    for row in reader:
        if all(not c.strip() for c in row):
            continue  # blank rows are skipped and not counted
        row_number += 1
        raw: dict[str, str] = {}
        for i, name in mapping:
            cell = row[i] if i < len(row) else ""
            if cell.strip() and not raw.get(name, "").strip():
                raw[name] = cell
        yield row_number, raw


# ---------------------------------------------------------------- persistence

@dataclass
class LeadFileOutcome:
    path: Path
    skipped: bool = False
    rows_total: int = 0
    rows_rejected: int = 0
    duplicates_merged: int = 0
    field_warnings: int = 0
    leads_created: int = 0


@dataclass
class _LeadState:
    lead_id: int
    attrs: dict
    pii_enc: bytes
    pii: dict | None = None  # decrypted lazily, only when a merge touches it


def _split(schema: WorkflowSchema, values: dict[str, str | None]):
    """-> (non-PII attrs, PII attrs except phone). Phone is stored separately."""
    phone = schema.phone_field.name
    attrs = {f.name: values.get(f.name) for f in schema.fields if not f.pii and f.name != phone}
    pii = {f.name: values.get(f.name) for f in schema.fields if f.pii and f.name != phone}
    return attrs, pii


def _with_defaults(schema: WorkflowSchema, d: dict) -> dict:
    # Applying defaults at write time is equivalent to applying them after the
    # final merge: a default is only ever replaced by a later non-null value,
    # which is exactly what last-non-null-wins merging would produce.
    return {k: (v if v is not None else schema_default(schema, k)) for k, v in d.items()}


def schema_default(schema: WorkflowSchema, name: str):
    return next(f.default for f in schema.fields if f.name == name)


def ingest_lead_file(conn: sqlite3.Connection, codec: PiiCodec,
                     schema: WorkflowSchema, path: Path) -> LeadFileOutcome:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    wf, batch_id = schema.workflow_id, path.stem
    out = LeadFileOutcome(path)

    prior = conn.execute(
        "SELECT sha256 FROM source_file WHERE kind='leads' AND workflow_id=? AND batch_id=?",
        (wf, batch_id)).fetchone()
    if prior is not None:
        if prior["sha256"] != sha:
            raise BatchConflictError(
                f"{path}: batch {wf}/{batch_id} was already ingested with different content")
        out.skipped = True
        return out

    text = data.decode("utf-8-sig")
    ref_name = schema.reference_field.name
    phone_name = schema.phone_field.name

    conn.execute("BEGIN IMMEDIATE")
    try:
        file_id = conn.execute(
            "INSERT INTO source_file (kind, workflow_id, batch_id, path, sha256, ingested_at) "
            "VALUES ('leads', ?, ?, ?, ?, ?)",
            (wf, batch_id, str(path), sha, datetime.now(timezone.utc).isoformat())).lastrowid

        leads: dict[str, _LeadState] = {
            r["phone_index"]: _LeadState(r["lead_id"], json.loads(r["attrs_json"]), r["pii_enc"])
            for r in conn.execute(
                "SELECT lead_id, phone_index, attrs_json, pii_enc FROM lead WHERE workflow_id=?", (wf,))}

        for row_number, raw in iter_data_rows(text, schema):
            out.rows_total += 1
            res = validate_row(schema, raw)
            if res.values is None:
                out.rows_rejected += 1
                conn.execute(
                    "INSERT INTO lead_rejection (workflow_id, batch_id, row_number, reason, file_id) "
                    "VALUES (?,?,?,?,?)", (wf, batch_id, row_number, res.reason, file_id))
                continue

            out.field_warnings += len(res.warnings)
            phone = res.values[phone_name]
            idx = codec.blind_index(phone)
            attrs, pii = _split(schema, res.values)
            state = leads.get(idx)

            if state is None:
                attrs, pii = _with_defaults(schema, attrs), _with_defaults(schema, pii)
                pii_enc = codec.encrypt(json.dumps(pii, sort_keys=True))
                lead_id = conn.execute(
                    "INSERT INTO lead (workflow_id, phone_index, phone_enc, phone_last4, batch_id, "
                    "external_ref, attrs_json, pii_enc, created_file_id, updated_file_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (wf, idx, codec.encrypt(phone), phone[-4:], batch_id,
                     _ref(attrs, pii, ref_name), json.dumps(attrs, sort_keys=True),
                     pii_enc, file_id, file_id)).lastrowid
                leads[idx] = _LeadState(lead_id, attrs, pii_enc, pii)
                action = "created"
                out.leads_created += 1
            else:
                # Merged duplicate: overwrite only non-null fields (last write wins).
                if state.pii is None:
                    state.pii = json.loads(codec.decrypt(state.pii_enc))
                state.attrs.update({k: v for k, v in attrs.items() if v is not None})
                state.pii.update({k: v for k, v in pii.items() if v is not None})
                state.pii_enc = codec.encrypt(json.dumps(state.pii, sort_keys=True))
                conn.execute(
                    "UPDATE lead SET external_ref=?, attrs_json=?, pii_enc=?, updated_file_id=? "
                    "WHERE lead_id=?",
                    (_ref(state.attrs, state.pii, ref_name), json.dumps(state.attrs, sort_keys=True),
                     state.pii_enc, file_id, state.lead_id))
                action = "merged"
                out.duplicates_merged += 1

            conn.execute(
                "INSERT INTO lead_row (file_id, row_number, lead_id, action, warnings) VALUES (?,?,?,?,?)",
                (file_id, row_number, leads[idx].lead_id, action, ",".join(res.warnings)))

        conn.execute(
            "UPDATE source_file SET rows_total=?, rows_rejected=?, duplicates_merged=?, field_warnings=? "
            "WHERE file_id=?",
            (out.rows_total, out.rows_rejected, out.duplicates_merged, out.field_warnings, file_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return out


def _ref(attrs: dict, pii: dict, ref_name: str) -> str:
    return attrs.get(ref_name) if ref_name in attrs else pii[ref_name]
