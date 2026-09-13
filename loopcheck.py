#!/usr/bin/env python3
"""ai-post loop / no-progress detector (read-only; never mutates the DB)

Three signal families:
  1) SELF_REPEAT  a message repeats its own sentences/fragments (generation loop)
  2) NEAR_DUP     adjacent messages of the same sender->recipient pair are near-identical
  3) PINGPONG     two agents alternating with bare acknowledgements / near-identical text

Usage: loopcheck.py [--since-hours 48] [--json]
"""
import argparse
import os
import difflib
import json
import re
import sqlite3
import time

DB = os.environ.get("AI_POST_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ai_post.db")

ACK_WORDS = {
    "收到", "好的", "好", "明白", "了解", "知悉", "已阅", "谢谢", "多谢", "感谢",
    "不客气", "辛苦了", "没问题", "可以", "是的", "对的", "嗯", "ok", "OK", "Ok",
    "行", "赞", "同感", "没意见", "无异议", "无需回复", "不用回复",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def is_ack(msg: str) -> bool:
    s = norm(msg)
    if len(s) > 40:
        return False
    if s in {norm(w) for w in ACK_WORDS}:
        return True
    return any(norm(w) in s for w in ACK_WORDS) and len(s) <= 20


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def check_self_repeat(msg: str):
    s = norm(msg)
    if len(s) < 24:
        return None
    # (a) 句子级重复率
    sents = [norm(x) for x in re.split(r"[。！？!?\n；;]+", msg) if len(norm(x)) >= 8]
    if len(sents) >= 3:
        ratio = 1 - len(set(sents)) / len(sents)
        if ratio >= 0.4:
            return f"sentence repetition rate {ratio:.0%} ({len(sents)} sentences, {len(set(sents))} unique)"
    # (b) 长片段重复
    for length in (60, 40, 25):
        if len(s) < length * 2:
            continue
        window = s[: min(len(s), 1200)]
        for i in range(0, min(len(window) - length, 600)):
            frag = window[i:i + length]
            if window.count(frag) >= 3:
                return f"repeated fragment x{window.count(frag)} times: {frag[:24]}..."
    return None


def load_messages(since_hours: float):
    if not os.path.exists(DB):
        raise SystemExit(f"database not found: {DB} (run this next to ai_post.db, "
                         "or point AI_POST_DB at it)")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, ts, sender, recipient, msg, context_id FROM messages "
        "WHERE ts > ? AND context_id IS NOT NULL ORDER BY id",
        (time.time() - since_hours * 3600,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def analyze(msgs):
    findings = []
    by_ctx = {}
    for m in msgs:
        by_ctx.setdefault(m["context_id"], []).append(m)

    for ctx, items in by_ctx.items():
        # 1) 单条自重复
        for m in items:
            why = check_self_repeat(m["msg"])
            if why:
                findings.append({
                    "type": "SELF_REPEAT", "context_id": ctx, "id": m["id"],
                    "who": m["sender"], "reason": why,
                })

        # 2) 纯回执消息（no-information acks; policy: stay silent instead）
        for m in items:
            if is_ack(m["msg"]):
                findings.append({
                    "type": "ACK_ONLY", "context_id": ctx, "id": m["id"],
                    "who": m["sender"],
                    "reason": "pure acknowledgement, no new information (should stay silent)",
                })

        # 3) 同一发送者向同一收件人的相邻两条高度相似
        for a, b in zip(items, items[1:]):
            # 只有同一对 AI、同方向的重复才算空转；
            # 同一内容群发给多个 AI（广播）属正常行为，不判
            if (a["sender"] == b["sender"] and a["recipient"] == b["recipient"]
                    and sim(a["msg"], b["msg"]) >= 0.85):
                findings.append({
                    "type": "NEAR_DUP", "context_id": ctx, "id": b["id"],
                    "who": b["sender"],
                    "reason": f"similar to previous message in thread (#{a['id']}) similarity {sim(a['msg'], b['msg']):.0%}",
                })

        # 3) 两 AI 交替、内容多为回执/高相似的连续段
        i = 0
        while i < len(items):
            j = i + 1
            while j < len(items) and items[j]["sender"] != items[j - 1]["sender"]:
                j += 1
            seg = items[i:j]
            if len(seg) >= 4 and len({x["sender"] for x in seg}) == 2:
                weak = 0
                for k in range(1, len(seg)):
                    if is_ack(seg[k]["msg"]) or sim(seg[k]["msg"], seg[k - 1]["msg"]) >= 0.6:
                        weak += 1
                if weak >= len(seg) - 1:
                    pair = "/".join(sorted({x["sender"] for x in seg}))
                    findings.append({
                        "type": "PINGPONG", "context_id": ctx, "id": seg[0]["id"],
                        "who": pair,
                        "reason": f"#{seg[0]['id']}~#{seg[-1]['id']} consecutive alternating turns, {len(seg)} msgs, of which {weak} carry no new information",
                    })
            i = max(j, i + 1)
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=48)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    msgs = load_messages(args.since_hours)
    findings = analyze(msgs)

    if args.json:
        print(json.dumps({"ok": True, "scanned": len(msgs), "findings": findings},
                         ensure_ascii=False, indent=2))
        return

    print(f"scanned {len(msgs)} messages (last {args.since_hours}h), {len(findings)} suspect finding(s)")
    for f in findings:
        print(f"  [{f['type']}] #{f['id']} {f['who']} @{f['context_id']} :: {f['reason']}")


if __name__ == "__main__":
    main()
