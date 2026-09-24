"""Append-only CN research follow-ups in the existing workspace database."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .contracts import DISCLAIMER, ContractError, RunRequest, parse_datetime, require_string
from .feedback_metrics import HORIZONS, VERSION, _stats, evaluate_horizons, number, security_code
from .locking import run_lock
from .pipeline import verified_research_packet, verified_snapshot
from .reporting import _candidate_card
from .snapshot import load_snapshot
from .storage import connect, database_path, initialize_workspace
from .utils import sha256_value, write_text_atomic

SQL = """
CREATE TABLE IF NOT EXISTS cn_feedback_records (
 record_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cn_feedback_artifact ON cn_feedback_records(artifact_id, kind);
CREATE TRIGGER IF NOT EXISTS cn_feedback_no_update BEFORE UPDATE ON cn_feedback_records
 BEGIN SELECT RAISE(ABORT, 'feedback records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS cn_feedback_no_delete BEFORE DELETE ON cn_feedback_records
 BEGIN SELECT RAISE(ABORT, 'feedback records are append-only'); END;
"""


def _now():
    return datetime.now(UTC)


def _fields(raw, allowed):
    if not isinstance(raw, dict) or set(raw) - set(allowed):
        raise ContractError("unsupported research-feedback request fields")


def _init(workspace):
    initialize_workspace(workspace)
    with run_lock(workspace, "workspace-database"), connect(database_path(workspace)) as db:
        db.executescript(SQL)


def _read(workspace, identifier):
    if not isinstance(identifier, str) or not identifier.startswith("cn-feedback-"):
        raise ContractError("invalid feedback record ID")
    with connect(database_path(workspace)) as db:
        row = db.execute("SELECT * FROM cn_feedback_records WHERE record_id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError("feedback record not found")
    data = json.loads(row["payload_json"])
    if sha256_value(data) != row["payload_hash"] or any(data[k] != row[k] for k in
                                                       ("record_id","artifact_id","kind")):
        raise RuntimeError("feedback record integrity mismatch")
    packet = verified_research_packet(workspace, data["artifact_id"])
    if packet["analysis_hash"] != data["analysis_hash"]:
        raise RuntimeError("feedback parent artifact changed")
    if data.get("supersedes_id"):
        with connect(database_path(workspace)) as db:
            parent = db.execute("SELECT payload_json, payload_hash FROM cn_feedback_records WHERE record_id=?",
                                (data["supersedes_id"],)).fetchone()
        if parent is None or sha256_value(json.loads(parent["payload_json"])) != data["parent_hash"]:
            raise RuntimeError("hypothesis parent changed")
    if data["kind"] == "review":
        _ensure_report(workspace,data)
    return data


def _ensure_report(workspace, data):
    expected_path = f"reports/feedback/{data['record_id']}.md"
    if data.get("report_path") != expected_path:
        raise RuntimeError("feedback report integrity path mismatch")
    path = workspace / expected_path
    if not path.resolve().is_relative_to(workspace.resolve()) or path.is_symlink():
        raise RuntimeError("feedback report integrity path escape")
    if path.exists():
        # Verify the frozen bytes against their stored hash, not today's renderer.
        if sha256_value(path.read_text(encoding="utf-8")) != data.get("report_content_hash"):
            raise RuntimeError("feedback report integrity mismatch")
    else:
        content = _report(data)
        if sha256_value(content) != data.get("report_content_hash"):
            raise RuntimeError("feedback report integrity: recovery requires the original renderer")
        # Recover an interrupted derived-file write only when it is byte-equivalent.
        write_text_atomic(path,content)


def _receipt(data, reused=False):
    return {k:data[k] for k in ("record_id","artifact_id","kind","mode","formal_sample","generated_at")} | {
        "record_hash":sha256_value(data), "readback_verified":True,"reused":reused,
        "report_path":data.get("report_path"), "publication":"not_published"}


def _save(workspace, packet, kind, stable):
    identifier = "cn-feedback-" + sha256_value({"version":VERSION,"artifact":packet["analysis_hash"],
                                              "kind":kind,"payload":stable})[:24]
    _init(workspace)
    with run_lock(workspace, identifier):
        with connect(database_path(workspace)) as db:
            exists = db.execute("SELECT 1 FROM cn_feedback_records WHERE record_id=?", (identifier,)).fetchone()
        if exists:
            return _receipt(_read(workspace, identifier), reused=True)
        data = stable | {"record_id":identifier,"artifact_id":packet["artifact_id"],
                         "analysis_hash":packet["analysis_hash"],"kind":kind,"version":VERSION,
                         "market":"CN","mode":"shadow","formal_sample":False,
                         "generated_at":_now().isoformat(),"publication":"not_published"}
        if kind == "review":
            data["report_path"] = f"reports/feedback/{identifier}.md"
            data["report_content_hash"] = sha256_value(_report(data))
        with connect(database_path(workspace)) as db:
            db.execute("INSERT INTO cn_feedback_records VALUES (?,?,?,?,?)",
                       (identifier,data["artifact_id"],kind,json.dumps(data,ensure_ascii=False,allow_nan=False),sha256_value(data)))
        checked = _read(workspace, identifier)
        return _receipt(checked)


def review_feedback(raw, workspace: Path):
    _fields(raw, {"artifact_id","snapshot_ids"})
    now = _now()
    packet = verified_research_packet(workspace, raw.get("artifact_id"))
    panel = packet.get("research_feedback")
    if not panel:
        raise ContractError("artifact predates feedback contract; create a new versioned run")
    ids = raw.get("snapshot_ids")
    if not isinstance(ids,list) or not 1 <= len(ids) <= 64 or any(not isinstance(i,str) for i in ids):
        raise ContractError("snapshot_ids must contain 1 to 64 explicit snapshot IDs")
    datasets = [verified_snapshot(workspace, packet["artifact_id"])]
    receipts = [{"snapshot_id":packet["snapshot_id"],"snapshot_hash":packet["snapshot_hash"],
                 "as_of":datasets[0]["as_of"],"retrieved_at":datasets[0]["retrieved_at"],"role":"frozen_anchor"}]
    for identifier in sorted(set(ids)):
        request = RunRequest.from_dict({"schema_version":"0.1","market":"CN","workflow":"daily_report",
            "decision_at":now.isoformat(),"snapshot":{"selector":"id","snapshot_id":identifier}})
        snapshot = load_snapshot(workspace,request)
        if snapshot.data_mode != packet["data_mode"]:
            raise ContractError("cannot mix fixture and real feedback")
        if parse_datetime(snapshot.data["retrieved_at"],"retrieved_at") > now:
            raise ContractError("feedback input retrieved in future")
        datasets.append(snapshot.data)
        receipts.append({"snapshot_id":snapshot.snapshot_id,"snapshot_hash":snapshot.snapshot_hash,
                         "as_of":snapshot.data["as_of"],"retrieved_at":snapshot.data["retrieved_at"]})
    candidates = packet["all_decisions"]
    codes = [security_code(c["security_id"]) for c in candidates]
    evaluation = evaluate_horizons(datasets,panel["anchor_session"],codes,panel["benchmark_code"],list(HORIZONS),now)
    by_code = {(r["code"],r["horizon_sessions"]):r for r in evaluation["outcomes"]}
    candidate_results = [{"candidate_id":c["candidate_id"],"original_decision":c["decision"],"original_candidate":c,
                          "theme_id":c["theme_id"],"horizons":[by_code[(security_code(c["security_id"]),h)]
                                                                 for h in HORIZONS]} for c in candidates]
    experiment = panel["experiment"]
    results = {c["candidate_id"]:c for c in candidate_results}
    comparisons = []
    for h in HORIZONS:
        arms = {}
        for arm in ("R0","R1"):
            selected = experiment[arm][:experiment["top_n"]]
            vals = [number(next(r for r in results[c]["horizons"] if r["horizon_sessions"]==h)["excess_pct"])
                    for c in selected]
            arms[arm] = {"candidate_ids":selected, "statistics":_stats(vals)}
        comparisons.append({"horizon_sessions":h, **arms, "efficacy":"UNKNOWN",
                            "note":"One run is not independent validation; missing members remain in denominators"})
    fixed_groups = {name:panel["cohorts"][name] for name in ("strong","weak","unclassified")}
    fixed_groups.update({"theme:"+t["theme_id"]:t["members"] for t in panel["themes"]})
    group_results = [
        {"group":name,"members":members,"horizon_sessions":h,
         "statistics":_stats([number(by_code[(code,h)]["excess_pct"]) for code in members])}
        for name,members in fixed_groups.items() for h in HORIZONS
    ]
    return _save(workspace,packet,"review",{
        "research_cutoff_at":packet["decision_at"],"anchor_session":panel["anchor_session"],
        "original_run_id":packet["run_id"],
        "input_snapshots":receipts,"sample_class":"fixture" if packet["data_mode"]=="fixture" else "reconstructed",
        "original_cohorts":panel["cohorts"],"candidate_results":candidate_results,
        "fixed_group_results":group_results,
        "evaluation":evaluation,"comparison":comparisons,
        "interpretation":"Close anchor is a research comparison, not an executable price; no semantic adjudication",
    })


def register_hypothesis(raw, workspace: Path):
    allowed = {"artifact_id","candidate_id","kind","expectation","horizon_sessions","evaluation_at",
               "prior_evidence_refs","new_evidence_refs","counterexample","invalidation_condition",
               "next_catalyst_at","supersedes_id","revision_reason"}
    _fields(raw,allowed)
    now = _now()
    packet = verified_research_packet(workspace,raw.get("artifact_id"))
    candidate = next((c for c in packet["all_decisions"] if c["candidate_id"]==raw.get("candidate_id")),None)
    if candidate is None:
        raise ContractError("hypothesis must refer to a frozen candidate")
    hypothesis_kind = raw.get("kind")
    if hypothesis_kind not in {"price_feedback","business_fact"}:
        raise ContractError("hypothesis kind must be price_feedback or business_fact")
    fields = {}
    for key in ("expectation","counterexample","invalidation_condition"):
        fields[key] = require_string(raw.get(key),key)
        if len(fields[key]) > 2000:
            raise ContractError("hypothesis text exceeds limit")
    if hypothesis_kind == "price_feedback":
        h = raw.get("horizon_sessions")
        if isinstance(h,bool) or not isinstance(h,int) or h not in HORIZONS:
            raise ContractError("price feedback horizon must be 1, 5 or 20 sessions")
        fields["horizon_sessions"] = h
    elif "horizon_sessions" in raw:
        raise ContractError("business facts require an explicit evaluation_at, not a price horizon")
    if hypothesis_kind == "business_fact" or "evaluation_at" in raw:
        deadline = parse_datetime(raw.get("evaluation_at"),"evaluation_at")
        if deadline <= now:
            raise ContractError("new hypothesis evaluation_at must be in the future")
        fields["evaluation_at"] = deadline.isoformat()
    else:
        fields["evaluation_at"] = "UNKNOWN_CALENDAR"
    catalyst = raw.get("next_catalyst_at",candidate["next_catalyst_at"])
    fields["next_catalyst_at"] = parse_datetime(catalyst,"next_catalyst_at").isoformat()
    fields["catalyst_expired_at_registration"] = parse_datetime(catalyst,"next_catalyst_at") <= now
    for key in ("prior_evidence_refs","new_evidence_refs"):
        refs = raw.get(key,[])
        if not isinstance(refs,list) or any(not isinstance(r,str) or r not in candidate["usable_evidence_refs"] for r in refs):
            raise ContractError("hypothesis evidence must be usable in the frozen candidate")
        fields[key] = sorted(set(refs))
    fields["expectation_type"] = "researcher_expectation_not_market_consensus"
    fields["expectation_gap"] = "UNKNOWN" if not all(fields[k] for k in ("prior_evidence_refs","new_evidence_refs")) else "REQUIRES_HUMAN_COMPARISON"
    _init(workspace)
    if raw.get("supersedes_id"):
        parent = _read(workspace,raw["supersedes_id"])
        if parent["kind"] != "hypothesis" or parent["candidate_id"] != candidate["candidate_id"]:
            raise ContractError("revision must retain the original candidate identity")
        fields.update(supersedes_id=parent["record_id"],parent_hash=sha256_value(parent),
                      revision_reason=require_string(raw.get("revision_reason"),"revision_reason"))
    elif "revision_reason" in raw:
        raise ContractError("revision_reason requires supersedes_id")
    return _save(workspace,packet,"hypothesis",fields | {
        "candidate_id":candidate["candidate_id"],"hypothesis_kind":hypothesis_kind,
        "research_cutoff_at":packet["decision_at"],"semantic_result":"UNKNOWN",
        "registration_semantics":"Generated timestamp is first registration; never backdated to research cutoff",
        "historical_prediction_eligibility":"LATE_REGISTRATION_NOT_PRIOR_PREDICTION",
        "original_invalidation_conditions":candidate["invalidation_conditions"],
    })


def read_feedback(raw, workspace: Path):
    _fields(raw,{"record_id","offset","max_chars"})
    _init(workspace)
    data = _read(workspace,raw.get("record_id"))
    content = json.dumps(data,ensure_ascii=False,indent=2)
    offset, limit = raw.get("offset",0),raw.get("max_chars",12000)
    if any(isinstance(v,bool) or not isinstance(v,int) for v in (offset,limit)) or not 500 <= limit <= 20000 or not 0 <= offset <= len(content):
        raise ContractError("invalid feedback page bounds")
    end = min(len(content),offset+limit)
    return _receipt(data) | {"content":content[offset:end],"total_chars":len(content),
                            "offset":offset,"next_offset":end if end < len(content) else None,
                            "truncated":end < len(content)}


def list_feedback(raw, workspace: Path):
    _fields(raw,{"artifact_id"})
    packet = verified_research_packet(workspace,raw.get("artifact_id"))
    _init(workspace)
    with connect(database_path(workspace)) as db:
        rows = db.execute("SELECT record_id, kind FROM cn_feedback_records WHERE artifact_id=? ORDER BY record_id LIMIT 101",
                          (packet["artifact_id"],)).fetchall()
    return {"market":"CN","artifact_id":packet["artifact_id"],"records":[dict(r) for r in rows[:100]],
            "truncated":len(rows)>100}


def _report(data):
    lines = ["# A股研究反馈复盘", "", f"生成时间：{data['generated_at']}",
             f"原 decision_at：{data['research_cutoff_at']}；观察锚点：{data['anchor_session']}",
             "状态：shadow；formal_sample=false；语义确认 UNKNOWN。", "",
             "价格反馈采用逐交易日参考前收价连乘；不是总回报或可实现收益。",
             f"[原不可变研究报告](../../artifacts/runs/{data['original_run_id']}/report.md)",
             "", "| 候选 | 原研究状态 | 期限 | 到期交易日 | 数据状态 | 相对基准变化% |",
             "|---|---|---:|---|---|---:|"]
    for c in data["candidate_results"]:
        for r in c["horizons"]:
            lines.append(f"| {c['candidate_id']} | {c['original_decision']} | T+{r['horizon_sessions']} | "
                         f"{r['target_session'] or 'UNKNOWN'} | {r['status']} | {r['excess_pct'] or 'UNKNOWN'} |")
    lines.extend(["", "全部原始候选与排除对象均保留。反例、证伪条件、风险和待复核项见原不可变研究报告。",
                  "样本未成熟、缺失、停牌、冲突或交易日历不可核时不填零；排序对照效果仍为 UNKNOWN。",
                  "", "输入快照及时间："])
    lines.extend(f"- {s['snapshot_id']}：as_of={s['as_of']}；retrieved_at={s['retrieved_at']}" for s in data["input_snapshots"])
    lines.extend(["", "## 原冻结组的后续表现", "",
                  "| 固定组 | 期限 | 有效/全部 | 中位相对变化% | 最低相对变化% | 正值比例 |",
                  "|---|---|---|---:|---:|---:|"])
    for row in data["fixed_group_results"]:
        s = row["statistics"]
        lines.append(f"| {row['group']} | T+{row['horizon_sessions']} | {s['observed']}/{s['total']} | "
                     f"{s['median_excess_pct'] or 'UNKNOWN'} | {s['minimum_excess_pct'] or 'UNKNOWN'} | {s['positive_fraction'] or 'UNKNOWN'} |")
    lines.extend(["", "## 相同候选数量的排序对照", "",
                  "| 期限 | R0 有效/全部 | R0 中位相对变化% | R1 有效/全部 | R1 中位相对变化% | 效果 |",
                  "|---|---|---:|---|---:|---|"])
    for row in data["comparison"]:
        a,b = row["R0"]["statistics"],row["R1"]["statistics"]
        lines.append(f"| T+{row['horizon_sessions']} | {a['observed']}/{a['total']} | {a['median_excess_pct'] or 'UNKNOWN'} | "
                     f"{b['observed']}/{b['total']} | {b['median_excess_pct'] or 'UNKNOWN'} | UNKNOWN |")
    lines.extend(["", "## 原候选的证据与反方条件", ""])
    for index,c in enumerate(data["candidate_results"],1):
        lines.extend(_candidate_card(index,c["original_candidate"]))
    return "\n".join([*lines,"",DISCLAIMER,""])
