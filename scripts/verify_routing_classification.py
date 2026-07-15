#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""路由分类验证器：补 replay 不验 primary_workflow 的盲区。
对 jsonl 测试集带多轮上下文跑 process_agent_turn，对比 primary_workflow + commands + 写库安全。
用法: python scripts/verify_routing_classification.py evals/agent2/dialogues/xxx.jsonl [--report out.md]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent_core import DailySnapshot, process_agent_turn
from app.workflows.intake import IncomingMessageEnvelope, ActiveWorkflowTask


def _envelope(text: str, active: tuple = ()) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="verify", sender_name="tester", dingtalk_user_id="dt",
        source="routing_verify", raw_text=text, message_id="m",
        conversation_id="c", active_tasks=active,
    )


def verify_file(jsonl_path: str) -> dict:
    lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    records = []
    total = 0
    wf_match = 0
    miswrite = 0
    bias_cats: dict[str, int] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        did = d.get("dialogue_id", "?")
        snapshot = DailySnapshot()
        active: tuple = ()
        for t in d.get("turns", []):
            text = t.get("text", "")
            exp = t.get("expected", {})
            exp_wf = exp.get("primary_workflow", "")
            exp_cmds = exp.get("expected_commands", [])
            r = process_agent_turn(_envelope(text, active), daily_snapshot=snapshot)
            act_wf = r.routing.primary_workflow
            cmds = r.daily_commands
            wrote = [ic for c in cmds if c.should_write for ic in c.content]
            wrote_str = "、".join(wrote) if wrote else "未写入"
            exp_no_write = (
                exp_wf in ("chat", "internal_qa", "monthly_report")
                or "no_write" in exp_cmds
                or "begin_edit" in exp_cmds
            )
            unsafe = exp_no_write and bool(wrote)
            if unsafe:
                miswrite += 1
            wf_ok = act_wf == exp_wf
            if wf_ok:
                wf_match += 1
            cat = ""
            if not wf_ok:
                if unsafe:
                    cat = "误写"
                elif exp_no_write and not wrote:
                    cat = "行为安全(未误写)"
                elif ("开庭" in text) or ("后天" in text and "去" in text):
                    cat = "开庭/地名误判出差"
                elif "案件" in text or "项目" in text:
                    cat = "案件名干扰"
                else:
                    cat = "其他分类偏差"
                bias_cats[cat] = bias_cats.get(cat, 0) + 1
            records.append({
                "did": did, "tid": t.get("turn_id", ""), "text": text,
                "exp_wf": exp_wf, "act_wf": act_wf, "exp_cmds": exp_cmds,
                "act_ops": [c.operation for c in cmds],
                "wrote": wrote_str, "wf_ok": wf_ok, "unsafe": unsafe, "cat": cat,
            })
            total += 1
            snapshot = r.daily_after
            if act_wf == "daily_report" and any(c.should_write for c in cmds):
                active = (ActiveWorkflowTask(workflow="daily_report", task_id=f"{did}-d", status="collecting", reply_candidate=True),)
            elif act_wf == "daily_report" and active:
                pass
            else:
                active = ()

    return {
        "path": jsonl_path, "total": total, "wf_match": wf_match,
        "miswrite": miswrite, "bias_cats": bias_cats, "records": records,
    }


def render_md(result: dict) -> str:
    total = result["total"]
    wf_match = result["wf_match"]
    miswrite = result["miswrite"]
    md = [f"# 路由分类验证报告 · {Path(result['path']).name}\n"]
    md.append(f"> 总轮次 {total} | workflow精确 {wf_match} ({wf_match*100//total if total else 0}%) | 误写 {miswrite}\n")
    md.append("## 偏差分类\n")
    md.append("| 类型 | 数量 |")
    md.append("|------|------|")
    for cat, cnt in sorted(result["bias_cats"].items(), key=lambda x: -x[1]):
        md.append(f"| {cat} | {cnt} |")
    md.append(f"| (精确匹配) | {wf_match} |")
    md.append("\n## 误判明细\n")
    md.append("| 输入 | 期望 | 实际 | 指令 | 写入 | 类型 |")
    md.append("|------|------|------|------|------|------|")
    for x in result["records"]:
        if not x["wf_ok"]:
            md.append(f"| {x['text'][:24]} | {x['exp_wf']} | {x['act_wf']} | {x['act_ops']} | {x['wrote'][:18]} | {x['cat']} |")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser(description="路由分类验证器（补 replay 不验 primary_workflow）")
    ap.add_argument("jsonl", help="测试集 jsonl 路径")
    ap.add_argument("--report", default="", help="输出 md 报告路径（可选）")
    args = ap.parse_args()

    result = verify_file(args.jsonl)
    total = result["total"]
    wf_match = result["wf_match"]
    miswrite = result["miswrite"]
    print(f"文件: {args.jsonl}")
    print(f"总轮次: {total} | workflow精确: {wf_match} ({wf_match*100//total if total else 0}%) | 误写: {miswrite}")
    print(f"偏差分类: {result['bias_cats']}")
    if args.report:
        Path(args.report).write_text(render_md(result), encoding="utf-8")
        print(f"报告: {args.report}")


if __name__ == "__main__":
    main()
