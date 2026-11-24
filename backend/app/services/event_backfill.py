"""事件补录领域服务：外部机构补录文件的逐行核对、部分接收、幂等确认与覆盖人数发布。

设计要点（对应补录稳定性要求）：

1. 逐行核对：每一行都核对业务时间、归属（地区）和来源编号，坏数据进入拒收行文件，
   既不写入正式事件、也不按 0 计入任何聚合。
2. 部分接收：一个补录批次允许部分行接收、部分行拒收/待核；实际接收的行形成
   backfill_events，批次状态随确认进度在 部分接收 / 整批待核 / 整批接收 间流转。
3. 幂等：来源机构 + 来源编号 唯一。补交同一条事件时保留原文件的拒收原因，
   确认通过后也只写入一次，重复导入不会重复计数。
4. 实时聚合：窗口覆盖人数只按 backfill_events 中「实际接收」的事件计算，
   待核/拒收行一律不参与。
5. 市级估算：支持 partial_accepted（先用已核补录）与 hold_baseline（暂保旧估算）
   两种口径；未确认地区的文件不能进入正式地区分账。
6. 可回查：每次估算保存采用的批次 id、文件行号和窗口范围（scope_snapshot），
   可沿估算关联的批次核对到具体文件行。
"""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.database import db

# 拒收/待核原因代码（同时作为人可读说明的键）
REASON_MISSING_TIME = "业务时间缺失或无法解析"
REASON_MISSING_REGION = "归属地区缺失"
REASON_REGION_UNCONFIRMED = "归属地区待核"
REASON_MISSING_SOURCE_NO = "来源编号缺失"
REASON_MISSING_PERSON = "覆盖对象标识缺失"
REASON_DUPLICATE_REJECTED = "来源编号与原文件拒收行重复，沿用原拒收原因"

# 行状态
LINE_ACCEPTED = "已接收"
LINE_PENDING = "待核"
LINE_REJECTED = "已拒收"

# 批次状态
BATCH_PARTIAL = "部分接收"
BATCH_PENDING = "整批待核"
BATCH_FULL = "整批接收"

# 估算口径
POLICY_PARTIAL = "partial_accepted"   # 先用已核补录
POLICY_HOLD = "hold_baseline"         # 暂保旧估算

# 估算/分账类型
KIND_CITY_ESTIMATE = "市级估算"
KIND_REGIONAL_LEDGER = "正式地区分账"

# 拒收行文件输出目录（与 SQLite 数据目录并列，可通过环境变量覆盖）
REJECT_DIR = Path(os.environ.get("CC10045_REJECT_DIR", Path(__file__).resolve().parent.parent.parent / "data" / "rejects"))

# 补录文件允许的列名（支持中英文两种表头）
COLUMN_ALIASES = {
    "source_event_no": ("来源编号", "source_event_no", "event_no", "事件编号"),
    "business_time": ("业务时间", "business_time", "occurred_at", "事件时间"),
    "region": ("归属", "地区", "region", "area"),
    "region_confirmed": ("归属确认", "地区确认", "region_confirmed", "confirmed"),
    "person_key": ("覆盖对象", "人员编号", "person_key", "user_id"),
    "event_name": ("事件名称", "event_name", "name"),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_business_time(raw: str) -> str | None:
    """核对业务时间：支持 ISO 与 'YYYY-MM-DD HH:MM:SS' 两种常见格式。

    返回规范化后的时间字符串；无法解析返回 None（坏数据，不得当零处理）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    candidates = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for fmt in candidates:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S") if "%H" in fmt else dt.strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            continue
    return None


def _parse_bool(raw: Any) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "y", "是", "已确认", "确认")


def _pick(row: dict[str, Any], key: str) -> str:
    for alias in COLUMN_ALIASES[key]:
        if alias in row and str(row[alias]).strip() != "":
            return str(row[alias]).strip()
    return ""


class EventBackfillService:
    """事件补录摄入与覆盖人数发布服务。"""

    # ================================================================
    # 文件解析与逐行核对
    # ================================================================

    def parse_file(self, content: str) -> list[dict[str, str]]:
        """解析 CSV 补录文件为原始行字典列表。空文件抛出 ValueError。"""
        text = content.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("补录文件缺少表头")
        rows = [dict(row) for row in reader]
        if not rows:
            raise ValueError("补录文件没有数据行")
        return rows

    def import_backfill_file(
        self,
        content: str,
        *,
        source_org: str,
        source_file: str,
        shift_code: str = "",
        batch_no: str | None = None,
    ) -> dict[str, Any]:
        """导入外部机构补录文件：逐行核对并落库，返回实际接收批次与拒收行文件。

        整个导入在一个事务里执行：任何一行落库失败则整批回滚，避免半批次脏数据。
        """
        source_org = (source_org or "").strip()
        source_file = (source_file or source_file).strip()
        if not source_org:
            raise ValueError("来源机构不能为空")

        raw_rows = self.parse_file(content)
        batch_no = batch_no or f"BF_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        fingerprint = f"{source_org}:{source_file}:{len(content)}:{hash(content)}"

        with db.tx():
            batch_id = db.insert("backfill_batches", {
                "batch_no": batch_no,
                "source_org": source_org,
                "source_file": source_file,
                "file_fingerprint": fingerprint,
                "shift_code": shift_code,
                "status": BATCH_PENDING,
                "total_lines": len(raw_rows),
            })

            line_records: list[dict[str, Any]] = []
            for idx, raw in enumerate(raw_rows, start=1):
                record = self._validate_line(batch_id, idx, raw, source_org)
                line_id = db.insert("backfill_lines", record)
                record["id"] = line_id
                record["batch_no"] = batch_no
                line_records.append(record)

            # 核对通过且归属已确认的行，写入实际接收事件（幂等）
            accepted = 0
            for record in line_records:
                if record["line_status"] == LINE_ACCEPTED:
                    self._accept_line(batch_id, record)
                    accepted += 1

            rejected = sum(1 for r in line_records if r["line_status"] == LINE_REJECTED)
            pending = sum(1 for r in line_records if r["line_status"] == LINE_PENDING)
            status = self._derive_batch_status(accepted, rejected, pending, len(raw_rows))

            reject_path = ""
            if rejected or pending:
                reject_path = self._write_reject_file(batch_no, line_records)

            db.update("backfill_batches", {
                "status": status,
                "accepted_lines": accepted,
                "rejected_lines": rejected,
                "confirmed_lines": accepted,
                "rejected_file_path": reject_path,
                "updated_at": _now(),
            }, "id=?", [batch_id])

        return self.get_batch(batch_id)

    def _validate_line(self, batch_id: int, line_no: int, raw: dict[str, Any], source_org: str) -> dict[str, Any]:
        """逐行核对业务时间、归属、来源编号；坏数据给明确拒收原因，绝不静默置零。"""
        source_event_no = _pick(raw, "source_event_no")
        business_time_raw = _pick(raw, "business_time")
        region = _pick(raw, "region")
        region_confirmed = _parse_bool(_pick(raw, "region_confirmed"))
        person_key = _pick(raw, "person_key")
        event_name = _pick(raw, "event_name") or "补录事件"
        business_time = _parse_business_time(business_time_raw)

        payload = {
            "source_event_no": source_event_no,
            "business_time": business_time_raw,
            "region": region,
            "region_confirmed": region_confirmed,
            "person_key": person_key,
            "event_name": event_name,
        }
        raw_content = json.dumps(raw, ensure_ascii=False)

        reasons: list[str] = []
        if not source_event_no:
            reasons.append(REASON_MISSING_SOURCE_NO)
        if business_time is None:
            reasons.append(REASON_MISSING_TIME)
        if not person_key:
            reasons.append(REASON_MISSING_PERSON)

        line_status: str
        if reasons:
            # 硬错误：坏数据拒收，不进入聚合、不当零处理
            line_status = LINE_REJECTED
        elif not region:
            line_status = LINE_REJECTED
            reasons.append(REASON_MISSING_REGION)
        elif not region_confirmed:
            # 归属存在但尚未确认：软挂起，可后续补确认，绝不进入正式分账
            line_status = LINE_PENDING
            reasons.append(REASON_REGION_UNCONFIRMED)
        else:
            line_status = LINE_ACCEPTED

        # 幂等核对：来源机构+来源编号 已在历史批次出现
        duplicate_of = None
        if source_event_no:
            duplicate_of = db.fetchone(
                "SELECT bl.id AS line_id, bl.line_status, bl.reject_reason, bl.file_line_no, "
                "bb.batch_no, bb.source_file, be.id AS event_id "
                "FROM backfill_lines bl JOIN backfill_batches bb ON bl.batch_id=bb.id "
                "LEFT JOIN backfill_events be ON be.line_id=bl.id "
                "WHERE bb.source_org=? AND bl.source_event_no=? "
                "ORDER BY bl.id DESC LIMIT 1",
                [source_org, source_event_no],
            )

        if duplicate_of is not None:
            if duplicate_of["event_id"] is not None or duplicate_of["line_status"] == LINE_ACCEPTED:
                # 同一事件已确认接收：保持拒收并注明重复，确认后只计一次
                line_status = LINE_REJECTED
                reasons = [f"来源编号 {source_event_no} 已在批次 {duplicate_of['batch_no']} 确认接收，重复提交不重复计数"]
            elif line_status == LINE_REJECTED:
                # 补交同一条事件且本行仍是坏数据：保留原文件的拒收原因
                original = duplicate_of["reject_reason"] or "原文件已拒收"
                reasons.append(REASON_DUPLICATE_REJECTED)
                reasons.append(
                    f"原批次 {duplicate_of['batch_no']} 第 {duplicate_of['file_line_no']} 行拒收原因：{original}")
            elif line_status == LINE_PENDING:
                # 本行仍待核，关联历史记录后维持待核
                reasons.append(f"来源编号 {source_event_no} 在批次 {duplicate_of['batch_no']} 尚未确认接收")
            else:
                # 本行核对通过、历史行未确认：补交修正有效，允许接收（只计一次）；
                # 原拒收原因原样保留在原文件行记录中，审计链不断。
                line_status = LINE_ACCEPTED
                reasons = []

        return {
            "batch_id": batch_id,
            "file_line_no": line_no,
            "source_event_no": source_event_no,
            "business_time": business_time or "",
            "region": region,
            "region_confirmed": 1 if region_confirmed else 0,
            "person_key": person_key,
            "event_name": event_name,
            "payload": json.dumps(payload, ensure_ascii=False),
            "line_status": line_status,
            "reject_reason": "；".join(reasons),
            "raw_content": raw_content,
        }

    def _accept_line(self, batch_id: int, record: dict[str, Any]) -> int | None:
        """将已核对通过的行写入实际接收事件表；唯一约束兜底保证只计一次。"""
        source_org = self._batch_source_org(batch_id)
        existing = db.fetchone(
            "SELECT id FROM backfill_events WHERE source_org=? AND source_event_no=?",
            [source_org, record["source_event_no"]],
        )
        if existing is not None:
            return existing["id"]

        try:
            event_id = db.insert("backfill_events", {
                "batch_id": batch_id,
                "line_id": record["id"],
                "file_line_no": record["file_line_no"],
                "source_org": source_org,
                "source_event_no": record["source_event_no"],
                "business_time": record["business_time"],
                "region": record["region"],
                "person_key": record["person_key"],
                "event_name": record["event_name"],
                "payload": record["payload"],
                "event_status": "已确认",
            })
        except Exception:
            # 并发导入下 UNIQUE(source_org, source_event_no) 兜底：已存在则不重复计数
            existing = db.fetchone(
                "SELECT id FROM backfill_events WHERE source_org=? AND source_event_no=?",
                [source_org, record["source_event_no"]],
            )
            if existing is None:
                raise
            return existing["id"]
        db.update("backfill_lines", {"accepted_event_id": event_id, "updated_at": _now()},
                  "id=?", [record["id"]])
        return event_id

    def _new_member_count(self, window_start: str, window_end: str) -> tuple[int, set[str], list[dict[str, Any]]]:
        """窗口内实际接收补录事件对应的去重新成员。

        外部机构补录的对象按业务约定均为新成员；同一人在窗口内多条事件只计一次，
        重复导入受 source_org+source_event_no 唯一约束保护，不会产生新成员。
        """
        rows = db.fetchall(
            "SELECT batch_id, file_line_no, source_event_no, region, person_key, business_time "
            "FROM backfill_events WHERE business_time >= ? AND business_time <= ? "
            "ORDER BY batch_id, file_line_no",
            [window_start, window_end],
        )
        persons = {r["person_key"] for r in rows}
        basis = [
            {"batch_id": r["batch_id"], "file_line_no": r["file_line_no"],
             "source_event_no": r["source_event_no"], "region": r["region"],
             "person_key": r["person_key"], "business_time": r["business_time"]}
            for r in rows
        ]
        return len(persons), persons, basis

    def _batch_source_org(self, batch_id: int) -> str:
        row = db.fetchone("SELECT source_org FROM backfill_batches WHERE id=?", [batch_id])
        return row["source_org"] if row else ""

    @staticmethod
    def _derive_batch_status(accepted: int, rejected: int, pending: int, total: int) -> str:
        if accepted == total:
            return BATCH_FULL
        if accepted == 0:
            return BATCH_PENDING
        return BATCH_PARTIAL

    def _write_reject_file(self, batch_no: str, line_records: list[dict[str, Any]]) -> str:
        """生成拒收行文件：包含待核/拒收行的原始行内容、行号与拒收原因。"""
        REJECT_DIR.mkdir(parents=True, exist_ok=True)
        path = REJECT_DIR / f"{batch_no}_rejected.csv"
        bad = [r for r in line_records if r["line_status"] != LINE_ACCEPTED]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["文件行号", "行状态", "拒收/待核原因", "来源编号", "业务时间", "归属", "原始内容"])
        for r in bad:
            writer.writerow([
                r["file_line_no"], r["line_status"], r["reject_reason"],
                r["source_event_no"], r["business_time"], r["region"], r["raw_content"],
            ])
        path.write_text(buffer.getvalue(), encoding="utf-8")
        return str(path)

    # ================================================================
    # 待核行确认 / 补交处理
    # ================================================================

    def confirm_line(self, batch_id: int, file_line_no: int, *, region: str | None = None) -> dict[str, Any]:
        """确认一条待核行（通常是地区补齐后）。确认通过才写入实际接收事件，且只计一次。"""
        line = db.fetchone(
            "SELECT * FROM backfill_lines WHERE batch_id=? AND file_line_no=?",
            [batch_id, file_line_no],
        )
        if line is None:
            return {"ok": False, "message": f"批次 {batch_id} 第 {file_line_no} 行不存在"}
        if line["line_status"] == LINE_REJECTED:
            return {"ok": False, "message": f"该行已拒收：{line['reject_reason']}，不能直接确认"}
        if line["line_status"] == LINE_ACCEPTED:
            return {"ok": True, "message": "该行已确认接收，无需重复确认", "line_id": line["id"]}

        final_region = (region or line["region"] or "").strip()
        if not final_region:
            return {"ok": False, "message": REASON_MISSING_REGION}

        with db.tx():
            # 幂等兜底：同来源编号若已在其他批次确认，维持拒收不计数
            batch = db.fetchone("SELECT * FROM backfill_batches WHERE id=?", [batch_id])
            dup = db.fetchone(
                "SELECT id FROM backfill_events WHERE source_org=? AND source_event_no=?",
                [batch["source_org"], line["source_event_no"]],
            )
            if dup is not None:
                db.update("backfill_lines", {
                    "line_status": LINE_REJECTED,
                    "reject_reason": f"来源编号 {line['source_event_no']} 已确认接收，重复提交不重复计数",
                    "updated_at": _now(),
                }, "id=?", [line["id"]])
                return {"ok": False, "message": "同来源编号事件已接收，本行按重复拒收，不重复计数"}

            db.update("backfill_lines", {
                "line_status": LINE_ACCEPTED,
                "region": final_region,
                "region_confirmed": 1,
                "reject_reason": "",
                "updated_at": _now(),
            }, "id=?", [line["id"]])
            line.update({"line_status": LINE_ACCEPTED, "region": final_region, "region_confirmed": 1})
            self._accept_line(batch_id, line)
            self._refresh_batch_status(batch_id)

        return {"ok": True, "message": f"第 {file_line_no} 行已确认接收", "line_id": line["id"]}

    def confirm_batch_region(self, batch_id: int, region: str) -> dict[str, Any]:
        """补齐整批待核地区：把该批次所有待核行用同一地区确认（场景：第十条地区待核后补齐）。"""
        pending_lines = db.fetchall(
            "SELECT file_line_no FROM backfill_lines WHERE batch_id=? AND line_status=?",
            [batch_id, LINE_PENDING],
        )
        confirmed, failed = 0, []
        for row in pending_lines:
            result = self.confirm_line(batch_id, row["file_line_no"], region=region)
            if result["ok"]:
                confirmed += 1
            else:
                failed.append({"line": row["file_line_no"], "reason": result["message"]})
        return {"ok": True, "confirmed": confirmed, "failed": failed, "batch": self.get_batch(batch_id)}

    def _refresh_batch_status(self, batch_id: int) -> None:
        stats = db.fetchone(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN line_status=? THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN line_status=? THEN 1 ELSE 0 END) AS rejected, "
            "SUM(CASE WHEN line_status=? THEN 1 ELSE 0 END) AS pending "
            "FROM backfill_lines WHERE batch_id=?",
            [LINE_ACCEPTED, LINE_REJECTED, LINE_PENDING, batch_id],
        )
        status = self._derive_batch_status(
            stats["accepted"] or 0, stats["rejected"] or 0, stats["pending"] or 0, stats["total"] or 0,
        )
        reject_path = self._refresh_reject_file(batch_id)
        db.update("backfill_batches", {
            "status": status,
            "accepted_lines": stats["accepted"] or 0,
            "rejected_lines": stats["rejected"] or 0,
            "confirmed_lines": stats["accepted"] or 0,
            "rejected_file_path": reject_path,
            "updated_at": _now(),
        }, "id=?", [batch_id])

    def _refresh_reject_file(self, batch_id: int) -> str:
        """确认后重写拒收行文件：只保留仍待核/拒收的行；全部处理完则删除文件。"""
        batch = db.fetchone("SELECT batch_no, rejected_file_path FROM backfill_batches WHERE id=?", [batch_id])
        if batch is None:
            return ""
        bad = db.fetchall(
            "SELECT file_line_no, line_status, reject_reason, source_event_no, "
            "business_time, region, raw_content FROM backfill_lines "
            "WHERE batch_id=? AND line_status != ? ORDER BY file_line_no",
            [batch_id, LINE_ACCEPTED],
        )
        old_path = batch["rejected_file_path"]
        if not bad:
            if old_path:
                Path(old_path).unlink(missing_ok=True)
            return ""
        path = Path(old_path) if old_path else REJECT_DIR / f"{batch['batch_no']}_rejected.csv"
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["文件行号", "行状态", "拒收/待核原因", "来源编号", "业务时间", "归属", "原始内容"])
        for r in bad:
            writer.writerow([
                r["file_line_no"], r["line_status"], r["reject_reason"],
                r["source_event_no"], r["business_time"], r["region"], r["raw_content"],
            ])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(buffer.getvalue(), encoding="utf-8")
        return str(path)

    # ================================================================
    # 批次与行查询
    # ================================================================

    def list_batches(self, shift_code: str | None = None) -> list[dict[str, Any]]:
        if shift_code:
            return db.fetchall(
                "SELECT * FROM backfill_batches WHERE shift_code=? ORDER BY id DESC", [shift_code])
        return db.fetchall("SELECT * FROM backfill_batches ORDER BY id DESC")

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        batch = db.fetchone("SELECT * FROM backfill_batches WHERE id=?", [batch_id])
        if batch is None:
            raise ValueError(f"补录批次 {batch_id} 不存在")
        batch["lines"] = db.fetchall(
            "SELECT id, file_line_no, source_event_no, business_time, region, region_confirmed, "
            "person_key, event_name, line_status, reject_reason, accepted_event_id "
            "FROM backfill_lines WHERE batch_id=? ORDER BY file_line_no",
            [batch_id])
        batch["accepted_events"] = db.fetchall(
            "SELECT id, file_line_no, source_event_no, business_time, region, person_key, event_name "
            "FROM backfill_events WHERE batch_id=? ORDER BY file_line_no",
            [batch_id])
        return batch

    def get_reject_file_content(self, batch_id: int) -> str:
        batch = db.fetchone("SELECT rejected_file_path, batch_no FROM backfill_batches WHERE id=?", [batch_id])
        if batch is None or not batch["rejected_file_path"]:
            return ""
        path = Path(batch["rejected_file_path"])
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ================================================================
    # 实时聚合：按实际接收事件计算窗口覆盖人数
    # ================================================================

    def window_coverage(
        self,
        window_start: str,
        window_end: str,
        *,
        batch_ids: Iterable[int] | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        """统计窗口内实际接收事件的去重覆盖人数。

        只统计 backfill_events（已确认接收）中的事件；待核/拒收行不在此表，天然不计入。
        坏数据不会被当作 0：没有有效事件时返回 null 风格的空范围标记而非伪造数值。
        """
        where = ["business_time >= ?", "business_time <= ?"]
        params: list[Any] = [window_start, window_end]
        if batch_ids:
            ids = list(batch_ids)
            if ids:
                where.append(f"batch_id IN ({','.join('?' * len(ids))})")
                params.extend(ids)
        if region:
            where.append("region=?")
            params.append(region)

        where_clause = " AND ".join(where)
        rows = db.fetchall(
            f"SELECT DISTINCT person_key FROM backfill_events WHERE {where_clause}", params)
        line_rows = db.fetchall(
            f"SELECT batch_id, file_line_no, source_event_no, region, person_key, business_time "
            f"FROM backfill_events WHERE {where_clause} ORDER BY batch_id, file_line_no", params)

        return {
            "window_start": window_start,
            "window_end": window_end,
            "region": region,
            "coverage_count": len({r["person_key"] for r in rows}),
            "event_count": len(line_rows),
            "basis_lines": [
                {"batch_id": r["batch_id"], "file_line_no": r["file_line_no"],
                 "source_event_no": r["source_event_no"], "region": r["region"],
                 "person_key": r["person_key"], "business_time": r["business_time"]}
                for r in line_rows
            ],
            "has_effective_data": len(line_rows) > 0,
        }

    # ================================================================
    # 市级估算发布（两种口径）与正式地区分账
    # ================================================================

    def publish_city_estimate(
        self,
        *,
        shift_code: str,
        window_start: str,
        window_end: str,
        policy: str = POLICY_PARTIAL,
        baseline_count: int | None = None,
    ) -> dict[str, Any]:
        """发布本班市级覆盖人数估算，并冻结发布时采用的批次/行号范围以便回查。

        - partial_accepted：基线旧估算 + 窗口内所有实际接收补录事件的去重新增人数。
        - hold_baseline：暂保旧估算，不采用任何待核文件；待补齐有效文件后再发布。
        未确认地区的行不在 backfill_events 中，两种口径都不会把它们计入正式分账。
        """
        if policy not in (POLICY_PARTIAL, POLICY_HOLD):
            raise ValueError(f"未知估算口径：{policy}")

        baseline = self._resolve_baseline(shift_code, baseline_count)

        if policy == POLICY_HOLD:
            scope: list[dict[str, Any]] = []
            added_persons: set[str] = set()
            basis_text = "暂保旧估算，未采用待核补录文件"
        else:
            added_count, added_persons, scope = self._new_member_count(window_start, window_end)
            basis_text = f"基线 {baseline} 人 + 实际接收补录新增 {added_count} 人"

        total_count = baseline + len(added_persons)
        scope_snapshot = {
            "window_start": window_start,
            "window_end": window_end,
            "policy": policy,
            "baseline_count": baseline,
            "basis_batches": sorted({r["batch_id"] for r in scope}),
            "basis_lines": scope,
        }

        estimate_id = db.insert("coverage_estimates", {
            "shift_code": shift_code,
            "estimate_kind": KIND_CITY_ESTIMATE,
            "policy": policy,
            "coverage_count": total_count,
            "baseline_count": baseline,
            "added_count": len(added_persons),
            "window_start": window_start,
            "window_end": window_end,
            "scope_snapshot": json.dumps(scope_snapshot, ensure_ascii=False),
            "basis_text": basis_text,
            "status": "已发布",
        })
        return self.get_estimate(estimate_id)

    def publish_regional_ledgers(self, *, shift_code: str, window_start: str, window_end: str,
                                 estimate_id: int | None = None) -> dict[str, Any]:
        """生成正式地区分账：仅统计归属已确认的实际接收事件，按地区去重并留存文件行号。"""
        rows = db.fetchall(
            "SELECT batch_id, file_line_no, source_event_no, region, person_key, business_time "
            "FROM backfill_events WHERE business_time >= ? AND business_time <= ? "
            "ORDER BY region, batch_id, file_line_no",
            [window_start, window_end],
        )
        by_region: dict[str, dict[str, Any]] = {}
        for r in rows:  # backfill_events 只可能来自已确认归属的行
            bucket = by_region.setdefault(r["region"], {"persons": set(), "lines": [], "batches": set()})
            bucket["persons"].add(r["person_key"])
            bucket["lines"].append({"batch_id": r["batch_id"], "file_line_no": r["file_line_no"],
                                    "source_event_no": r["source_event_no"]})
            bucket["batches"].add(r["batch_id"])

        ledgers = []
        with db.tx():
            for region, bucket in sorted(by_region.items()):
                ledger_id = db.insert("region_ledgers", {
                    "shift_code": shift_code,
                    "region": region,
                    "coverage_count": len(bucket["persons"]),
                    "basis_batch_ids": json.dumps(sorted(bucket["batches"])),
                    "basis_line_refs": json.dumps(bucket["lines"], ensure_ascii=False),
                    "estimate_id": estimate_id,
                    "status": KIND_REGIONAL_LEDGER,
                })
                ledgers.append(db.fetchone("SELECT * FROM region_ledgers WHERE id=?", [ledger_id]))
        return {"ok": True, "ledgers": ledgers, "excluded_note": "归属待核/拒收行不进入正式地区分账"}

    def _resolve_baseline(self, shift_code: str, baseline_count: int | None) -> int:
        if baseline_count is not None:
            return int(baseline_count)
        # 基线只能回溯最初登记的「已确认旧估算」，不能把上一轮已含补录增量的估算当基线，
        # 否则重复发布会把同一批新成员反复叠加。
        baseline_row = db.fetchone(
            "SELECT coverage_count FROM coverage_estimates WHERE shift_code=? AND policy='baseline' "
            "ORDER BY id DESC LIMIT 1",
            [shift_code],
        )
        if baseline_row is not None:
            return int(baseline_row["coverage_count"])
        latest = db.fetchone(
            "SELECT coverage_count FROM coverage_estimates WHERE shift_code=? AND estimate_kind=? "
            "ORDER BY id DESC LIMIT 1",
            [shift_code, KIND_CITY_ESTIMATE],
        )
        return int(latest["coverage_count"]) if latest else 0

    def get_estimate(self, estimate_id: int) -> dict[str, Any]:
        row = db.fetchone("SELECT * FROM coverage_estimates WHERE id=?", [estimate_id])
        if row is None:
            raise ValueError(f"估算记录 {estimate_id} 不存在")
        row["scope"] = json.loads(row["scope_snapshot"])
        return row

    def latest_estimate(self, shift_code: str) -> dict[str, Any] | None:
        row = db.fetchone(
            "SELECT * FROM coverage_estimates WHERE shift_code=? ORDER BY id DESC LIMIT 1",
            [shift_code])
        if row is None:
            return None
        row["scope"] = json.loads(row["scope_snapshot"])
        return row

    def list_estimates(self, shift_code: str | None = None) -> list[dict[str, Any]]:
        if shift_code:
            rows = db.fetchall(
                "SELECT * FROM coverage_estimates WHERE shift_code=? ORDER BY id DESC", [shift_code])
        else:
            rows = db.fetchall("SELECT * FROM coverage_estimates ORDER BY id DESC")
        for row in rows:
            row["scope"] = json.loads(row["scope_snapshot"])
        return rows

    def list_ledgers(self, shift_code: str | None = None) -> list[dict[str, Any]]:
        if shift_code:
            return db.fetchall("SELECT * FROM region_ledgers WHERE shift_code=? ORDER BY id", [shift_code])
        return db.fetchall("SELECT * FROM region_ledgers ORDER BY id")

    # ================================================================
    # 值班基线登记：把已确认的旧估算（如 30 人）固化为可回查的基线
    # ================================================================

    def register_baseline(self, *, shift_code: str, baseline_count: int,
                          window_start: str, window_end: str, note: str = "已确认旧估算") -> dict[str, Any]:
        estimate_id = db.insert("coverage_estimates", {
            "shift_code": shift_code,
            "estimate_kind": KIND_CITY_ESTIMATE,
            "policy": "baseline",
            "coverage_count": int(baseline_count),
            "baseline_count": int(baseline_count),
            "added_count": 0,
            "window_start": window_start,
            "window_end": window_end,
            "scope_snapshot": json.dumps({
                "window_start": window_start, "window_end": window_end,
                "policy": "baseline", "baseline_count": int(baseline_count),
                "basis_batches": [], "basis_lines": [], "note": note,
            }, ensure_ascii=False),
            "basis_text": f"{note}：{baseline_count} 人",
            "status": "已发布",
        })
        return self.get_estimate(estimate_id)


backfill_service = EventBackfillService()
