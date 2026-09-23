#!/usr/bin/env python3
"""把 CVE 自動修補迴圈（cve-loop.sh）留下的機械記錄彙整成一份 Excel 報表。

資料來源優先順序：
  1. .cve-loop-history.jsonl —— cve-loop.sh 自己在每次 start-round /
     trigger-build 時寫下的結構化記錄，是最準確的來源。
  2. pipeline-logs/round<N>-attempt<M>.log —— 在 history 檔案還沒涵蓋到的
     舊資料（例如加入這個記錄功能之前就已經跑過的輪次），盡量從裡面回溯
     runId / 結果，並用同樣的規則去 pipeline-artifacts 找 Trivy 報告算
     fixable/unfixable。這條路徑拿不到 commit/變更檔案等資訊，報表裡會
     老實標成「回溯資料，無法取得變更明細」，不用猜的。

輸出三個分頁，格式參考使用者提供的 ai-patch-report-*.xlsx 範例：
  - 執行總覽
  - 逐輪嘗試
  - CVE 追蹤
"""
import argparse
import glob
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

SUCCESS_RESULTS = {"succeeded", "succeededWithIssues", "partiallySucceeded"}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]


# ---------------------------------------------------------------- utils --

def to_int(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    v = str(v).strip()
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def parse_ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def fmt_ts(dt):
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 0:
        return "-"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小時 {m} 分 {s} 秒"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def run_git(repo_dir, *args):
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        return None


# ------------------------------------------------------- history loading --

def load_history(workspace: Path):
    """讀 .cve-loop-history.jsonl，回傳 (start_rounds, attempts)。
    start_rounds: {outer_round: {...}}
    attempts: {(outer_round, build_attempt): {...}}（同一個 key 若重複記錄，
    以最後一筆為準——理論上不會發生，但這樣比較保險）
    """
    history_file = workspace / ".cve-loop-history.jsonl"
    start_rounds, attempts = {}, {}
    if not history_file.exists():
        return start_rounds, attempts

    for line in history_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "start_round":
            r = to_int(rec.get("outer_round"))
            if r is not None:
                start_rounds[r] = rec
        elif rec.get("event") == "build_attempt":
            r = to_int(rec.get("outer_round"))
            a = to_int(rec.get("build_attempt"))
            if r is not None and a is not None:
                attempts[(r, a)] = rec
    return start_rounds, attempts


def find_trivy_json(artifacts_dir: Path):
    if not artifacts_dir or not artifacts_dir.is_dir():
        return None
    for p in sorted(artifacts_dir.rglob("*.json")):
        if "trivy-report" in str(p).lower():
            return p
    return None


def load_trivy_snapshot(trivy_json_path):
    """回傳 {(cve_id, pkg): {severity, installed, fixed_versions:set, source}}"""
    snapshot = {}
    if not trivy_json_path:
        return snapshot
    try:
        data = json.loads(Path(trivy_json_path).read_text())
    except (OSError, json.JSONDecodeError):
        return snapshot
    for result in data.get("Results") or []:
        source = result.get("Target") or result.get("Type") or "-"
        for v in result.get("Vulnerabilities") or []:
            key = (v.get("VulnerabilityID"), v.get("PkgName"))
            if not all(key):
                continue
            entry = snapshot.setdefault(key, {
                "severity": v.get("Severity") or "UNKNOWN",
                "installed": v.get("InstalledVersion") or "-",
                "fixed_versions": [],
                "source": source,
            })
            fixed = v.get("FixedVersion")
            if fixed and fixed not in entry["fixed_versions"]:
                entry["fixed_versions"].append(fixed)
    return snapshot


def count_fixable_unfixable(snapshot):
    fixable = sum(1 for v in snapshot.values() if v["fixed_versions"])
    unfixable = sum(1 for v in snapshot.values() if not v["fixed_versions"])
    return fixable, unfixable


LOG_NAME_RE = re.compile(r"round(\d+)-attempt(\d+)\.log$")


def reconstruct_from_logs(workspace: Path, attempts: dict, max_build_attempts: int):
    """補齊 history 檔案沒有涵蓋到的舊輪次（在加入 history 記錄功能之前就已
    經跑過的 round/attempt）。只從既有機械檔案（log 檔名、log 內容、
    pipeline-artifacts）推導，推不出來的欄位一律留 None，不用猜的。
    """
    logs_dir = workspace / "pipeline-logs"
    if not logs_dir.is_dir():
        return

    found = {}
    for log_path in logs_dir.glob("round*-attempt*.log"):
        m = LOG_NAME_RE.search(log_path.name)
        if not m:
            continue
        r, a = int(m.group(1)), int(m.group(2))
        if (r, a) in attempts:
            continue  # history 已經有正確記錄，不要用回溯的蓋掉
        found[(r, a)] = log_path

    for (r, a), log_path in found.items():
        text = log_path.read_text(errors="replace")
        run_id_m = re.search(r"runId\s*=\s*(\d+)", text)
        run_id = run_id_m.group(1) if run_id_m else None
        result_matches = re.findall(r"result=(\S+)", text)
        pipeline_result = result_matches[-1] if result_matches else None

        mtime = datetime.fromtimestamp(log_path.stat().st_mtime).astimezone()

        rec = {
            "event": "build_attempt",
            "outer_round": r,
            "build_attempt": a,
            "run_id": run_id,
            "ts_start": None,
            "ts_end": mtime.isoformat(),
            "duration_seconds": None,
            "commit": None,
            "_reconstructed": True,
        }

        if pipeline_result in SUCCESS_RESULTS and run_id:
            artifacts_dir = workspace / "pipeline-artifacts" / run_id
            trivy_json = find_trivy_json(artifacts_dir)
            if trivy_json:
                snapshot = load_trivy_snapshot(trivy_json)
                fixable, unfixable = count_fixable_unfixable(snapshot)
                rec.update(result="BUILD_OK", trivy_json=str(trivy_json),
                           fixable=fixable, unfixable=unfixable)
            else:
                rec.update(result="BUILD_OK_NO_TRIVY_REPORT")
        else:
            # 回溯資料分不出「這輪還會重試」跟「重試次數已用完回退」，
            # 用「是不是這個 round 目前看到的最後一次嘗試、且次數達上限」
            # 做最保守的推斷。
            round_attempts = [k[1] for k in found if k[0] == r] + [
                k[1] for k in attempts if k[0] == r
            ]
            is_last = a == max(round_attempts) if round_attempts else True
            if is_last and a >= max_build_attempts:
                rec["result"] = "BUILD_FAILED_GIVE_UP"
            else:
                rec["result"] = "BUILD_FAILED"

        attempts[(r, a)] = rec


# ------------------------------------------------------------- assembly --

def build_rounds_table(start_rounds, attempts, repo_dir):
    """回傳排序後的 attempt 記錄 list，每筆補上 before/after fixable、
    reduction、變更檔案、變更行數、說明。"""
    rows = []
    baseline_fixable = None
    baseline_unfixable = None
    prev_commit_in_round = {}

    for key in sorted(attempts.keys()):
        r, a = key
        rec = dict(attempts[key])
        rec["outer_round"], rec["build_attempt"] = r, a

        before_fixable, before_unfixable = baseline_fixable, baseline_unfixable
        result = rec.get("result")
        fixable = to_int(rec.get("fixable"))
        unfixable = to_int(rec.get("unfixable"))

        reduction = None
        if result == "BUILD_OK":
            if baseline_fixable is not None and fixable is not None:
                reduction = baseline_fixable - fixable
            baseline_fixable, baseline_unfixable = fixable, unfixable

        # 變更檔案 / 變更行數：只有 history 記錄（非回溯）且有 commit 時才算得出來。
        #
        # 每一輪的第一次嘗試，測的是上一輪結束時已經 commit 好的程式碼（也就是
        # 這一輪 start_round 記錄的 base_commit），這時候 commit 欄位一定跟
        # base_commit 相同，逐次嘗試間的「前一次 commit」比對自然算不出 diff——
        # 這是正常的（代表這次建置本來就還沒套用任何新修補）。
        #
        # 真正「修了什麼」的那個 commit，是這次 BUILD_OK 之後才 patch_cves()／
        # commit() 出來的，從來沒有被單獨拿去 trigger-build 測過，只會以「下一輪
        # start_round 的 base_commit」的身分出現。所以 BUILD_OK 這一列要展示的
        # diff，必須是「這一輪一開始的 base_commit」→「下一輪一開始的
        # base_commit」（如果這是最後一輪、流程就在這裡結束，則用這次自己的
        # commit 當終點，因為那已經是最終測過的狀態）。
        changed_files, diff_stat = None, None
        commit = rec.get("commit")
        if commit and not rec.get("_reconstructed"):
            if result == "BUILD_OK":
                base_rec = start_rounds.get(r)
                round_start_commit = base_rec.get("base_commit") if base_rec else None
                next_round_rec = start_rounds.get(r + 1)
                end_commit = next_round_rec.get("base_commit") if next_round_rec else commit
                diff_from, diff_to = round_start_commit, end_commit
            else:
                prev_commit = prev_commit_in_round.get(r)
                if prev_commit is None:
                    base_rec = start_rounds.get(r)
                    prev_commit = base_rec.get("base_commit") if base_rec else None
                diff_from, diff_to = prev_commit, commit

            if diff_from and diff_to and diff_from != diff_to:
                stat = run_git(repo_dir, "diff", "--stat", diff_from, diff_to)
                shortstat = run_git(repo_dir, "diff", "--shortstat", diff_from, diff_to)
                if stat is not None:
                    file_lines = [l for l in stat.splitlines() if "|" in l]
                    changed_files = ", ".join(
                        l.split("|")[0].strip() for l in file_lines
                    ) or "-"
                diff_stat = shortstat.strip() if shortstat else None
            prev_commit_in_round[r] = commit

        commit_msg = None
        if commit and not rec.get("_reconstructed"):
            commit_msg = run_git(repo_dir, "log", "-1", "--format=%s", commit)

        rec.update(
            before_fixable=before_fixable,
            before_unfixable=before_unfixable,
            after_fixable=fixable,
            reduction=reduction,
            changed_files=changed_files,
            diff_stat=diff_stat,
            commit_msg=commit_msg,
        )
        rows.append(rec)

    return rows


def describe_result(rec):
    result = rec.get("result")
    reconstructed = rec.get("_reconstructed")
    tail = "（回溯資料，無法取得變更明細）" if reconstructed else ""
    if result == "BUILD_OK":
        fixable = rec.get("after_fixable")
        if fixable == 0:
            base = "已推送 last-good，CVE 修補完成"
        else:
            base = f"已推送 last-good，剩餘 {fixable} 個可修復 CVE 進入下一輪"
    elif result == "BUILD_OK_NO_TRIVY_REPORT":
        base = "建置成功但找不到 Trivy 報告，需人工確認 artifact 結構"
    elif result == "BUILD_FAILED_GIVE_UP":
        reason = rec.get("reason")
        if reason == "PIPELINE_INFRA_ERROR":
            base = "本輪重試已達上限，且疑似基礎設施問題，已回退至 last-good"
        else:
            base = "本輪重試已達上限，已回退至 last-good，換一輪重試"
    elif result == "BUILD_FAILED":
        reason = rec.get("reason")
        if reason == "PIPELINE_INFRA_ERROR":
            base = "觸發/等待 pipeline 失敗（疑似基礎設施問題）"
        else:
            base = "建置失敗，build log 已回饋給下一輪修補"
    else:
        base = result or "-"
    msg = rec.get("commit_msg")
    if msg and not reconstructed:
        commit = rec.get("commit")
        short_commit = commit[:7] if commit else "-"
        base = f"{base}；commit ({short_commit}): {msg}"
    return base + tail


RESULT_LABELS = {
    "BUILD_OK": "建置成功",
    "BUILD_OK_NO_TRIVY_REPORT": "建置成功（缺 Trivy 報告）",
    "BUILD_FAILED": "建置失敗",
    "BUILD_FAILED_GIVE_UP": "建置失敗（本輪放棄）",
}


def build_cve_tracking(rounds_rows, workspace: Path):
    """走過每一次「有 Trivy 資料」的快照，追蹤每個 (cve, pkg) 的狀態。"""
    snapshots = []  # [(label, snapshot_dict)]
    for rec in rounds_rows:
        if rec.get("result") != "BUILD_OK":
            continue
        trivy_json = rec.get("trivy_json")
        if not trivy_json:
            continue
        label = f"第 {rec['outer_round']} 輪第 {rec['build_attempt']} 次嘗試"
        snapshots.append((label, load_trivy_snapshot(trivy_json)))

    if not snapshots:
        return [], None, None

    first_label, first_snapshot = snapshots[0]
    last_label, last_snapshot = snapshots[-1]

    all_keys = {}
    last_seen_idx = {}
    for idx, (label, snap) in enumerate(snapshots):
        for key, info in snap.items():
            all_keys[key] = info  # 保留看到的最後一次資訊（版本/嚴重性可能會變）
            last_seen_idx[key] = idx

    rows = []
    for key, info in all_keys.items():
        cve_id, pkg = key
        in_latest = key in last_snapshot
        if in_latest:
            status = "仍存在" if last_snapshot[key]["fixed_versions"] else "不可修復"
            disappeared_at = "-"
            latest_info = last_snapshot[key]
        else:
            status = "已修復"
            fixed_idx = last_seen_idx[key] + 1
            disappeared_at = (
                snapshots[fixed_idx][0] if fixed_idx < len(snapshots) else "-"
            ) + "（建置後）"
            latest_info = info
        rows.append({
            "cve": cve_id,
            "pkg": pkg,
            "severity": latest_info["severity"],
            "installed": latest_info["installed"],
            "fixed": ", ".join(latest_info["fixed_versions"]) if latest_info["fixed_versions"] else "-",
            "source": latest_info["source"],
            "status": status,
            "disappeared_at": disappeared_at,
        })

    def sort_key(row):
        status_rank = {"仍存在": 0, "不可修復": 1, "已修復": 2}[row["status"]]
        sev_rank = SEVERITY_ORDER.index(row["severity"]) if row["severity"] in SEVERITY_ORDER else len(SEVERITY_ORDER)
        return (status_rank, sev_rank, row["cve"])

    rows.sort(key=sort_key)
    return rows, snapshots[0], snapshots[-1]


# ------------------------------------------------------------- workbook --

NAVY = "1F3864"
WHITE = "FFFFFF"
GREEN_FILL = "C6EFCE"
GREEN_FONT = "006100"
RED_FILL = "FFC7CE"
RED_FONT = "9C0006"
YELLOW_FILL = "FFEB9C"
YELLOW_FONT = "9C5700"
GRAY_FILL = "E7E6E6"
GRAY_FONT = "3F3F3F"
BORDER_COLOR = "BFBFBF"

THIN = Side(style="thin", color=BORDER_COLOR)
CELL_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_header_row(ws: Worksheet, row: int, ncols: int, height=22):
    ws.row_dimensions[row].height = height
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.border = CELL_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")


def plain_cell(ws, row, col, value, align="left", wrap=False):
    cell = ws.cell(row=row, column=col, value=value)
    cell.border = CELL_BORDER
    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
    return cell


def badge_cell(ws, row, col, value, fg_color, font_color, align="center"):
    cell = plain_cell(ws, row, col, value, align=align)
    cell.fill = PatternFill("solid", fgColor=fg_color)
    cell.font = Font(bold=True, color=font_color)
    return cell


def build_sheet1(wb, meta):
    ws = wb.active
    ws.title = "執行總覽"
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 30

    ws.cell(row=1, column=1, value="AI Trivy 修補執行總覽").font = Font(bold=True, size=14, color=NAVY)

    def label_value(row, label, value, highlight=False):
        lc = ws.cell(row=row, column=1, value=label)
        lc.font = Font(bold=True)
        vc = ws.cell(row=row, column=2, value=value)
        if highlight:
            vc.font = Font(bold=True, color=GREEN_FONT, size=12)
            vc.fill = PatternFill("solid", fgColor=GREEN_FILL)
        vc.alignment = Alignment(horizontal="left")

    label_value(3, "執行時間", meta["start_time"])
    label_value(4, "總耗時", meta["total_duration"])
    label_value(5, "掃描嚴重性", meta["severities"])
    label_value(6, "目標分支", meta["nightly_branch"])
    label_value(7, "安全檢查點分支", meta["last_good_branch"])
    label_value(9, "起始可修復 CVE 數", meta["start_fixable"])
    label_value(10, "最終可修復 CVE 數", meta["final_fixable"])
    label_value(11, "本次修復數量", meta["fixed_count"], highlight=True)
    label_value(12, "修復率", meta["fix_rate"])
    label_value(13, "不可修復 CVE 數 (無 FixedVersion)", meta["final_unfixable"])
    label_value(15, "輪數上限", meta["max_rounds"])
    label_value(16, "實際執行輪數", meta["rounds_executed"])
    label_value(17, "每輪嘗試次數上限", meta["max_build_attempts"])
    label_value(18, "建置成功次數", meta["success_count"])
    label_value(19, "建置失敗次數", meta["failed_count"])
    if meta.get("stopped_reason"):
        label_value(21, "結束狀態", meta["stopped_reason"])


def build_sheet2(wb, rounds_rows):
    ws = wb.create_sheet("逐輪嘗試")
    headers = ["輪", "嘗試", "修補前\n可修復", "修補前\n不可修復", "結果",
               "修補後\n可修復", "本輪減少", "耗時", "變更檔案", "變更行數", "說明"]
    widths = [6, 6, 12, 12, 22, 12, 10, 16, 34, 16, 60]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for c, h in enumerate(headers, start=1):
        ws.cell(row=1, column=c, value=h)
    style_header_row(ws, 1, len(headers))
    ws.freeze_panes = "A2"

    row = 2
    for rec in rounds_rows:
        result = rec.get("result")
        plain_cell(ws, row, 1, rec["outer_round"], align="center")
        plain_cell(ws, row, 2, rec["build_attempt"], align="center")
        plain_cell(ws, row, 3, rec["before_fixable"] if rec["before_fixable"] is not None else "-", align="center")
        plain_cell(ws, row, 4, rec["before_unfixable"] if rec["before_unfixable"] is not None else "-", align="center")

        label = RESULT_LABELS.get(result, result or "-")
        if result == "BUILD_OK":
            if rec.get("after_fixable") == 0:
                badge_cell(ws, row, 5, "建置成功 - 全部修復完成", GREEN_FILL, GREEN_FONT)
            else:
                badge_cell(ws, row, 5, "建置成功 - 仍有可修復 CVE", YELLOW_FILL, YELLOW_FONT)
        elif result in ("BUILD_FAILED", "BUILD_FAILED_GIVE_UP"):
            badge_cell(ws, row, 5, label, RED_FILL, RED_FONT)
        else:
            badge_cell(ws, row, 5, label, GRAY_FILL, GRAY_FONT)

        plain_cell(ws, row, 6, rec["after_fixable"] if rec["after_fixable"] is not None else "-", align="center")
        reduction = rec.get("reduction")
        rc = plain_cell(ws, row, 7, reduction if reduction is not None else "-", align="center")
        if reduction:
            rc.font = Font(bold=True, color=GREEN_FONT)

        plain_cell(ws, row, 8, fmt_duration(rec.get("duration_seconds")), align="center")
        plain_cell(ws, row, 9, rec.get("changed_files") or "-", wrap=True)
        plain_cell(ws, row, 10, rec.get("diff_stat") or "-")
        plain_cell(ws, row, 11, describe_result(rec), wrap=True)
        row += 1


STATUS_STYLE = {
    "已修復": (GREEN_FILL, GREEN_FONT),
    "仍存在": (YELLOW_FILL, YELLOW_FONT),
    "不可修復": (GRAY_FILL, GRAY_FONT),
}


def build_sheet3(wb, cve_rows):
    ws = wb.create_sheet("CVE 追蹤")
    headers = ["CVE", "套件", "嚴重性", "目前版本", "修復版本", "來源", "狀態", "消失於"]
    widths = [20, 34, 12, 18, 22, 14, 12, 26]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for c, h in enumerate(headers, start=1):
        ws.cell(row=1, column=c, value=h)
    style_header_row(ws, 1, len(headers))
    ws.freeze_panes = "A2"

    row = 2
    for r in cve_rows:
        plain_cell(ws, row, 1, r["cve"])
        plain_cell(ws, row, 2, r["pkg"])
        plain_cell(ws, row, 3, r["severity"], align="center")
        plain_cell(ws, row, 4, r["installed"])
        plain_cell(ws, row, 5, r["fixed"])
        plain_cell(ws, row, 6, r["source"], align="center")
        fg, fc = STATUS_STYLE[r["status"]]
        badge_cell(ws, row, 7, r["status"], fg, fc)
        plain_cell(ws, row, 8, r["disappeared_at"], align="center")
        row += 1

    if row > 2:
        ws.auto_filter.ref = f"A1:H{row - 1}"


# ------------------------------------------------------------------ main --

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--nightly-branch", default="nightly-build")
    ap.add_argument("--last-good-branch", default="last-good")
    ap.add_argument("--max-rounds", type=int, default=10)
    ap.add_argument("--max-build-attempts", type=int, default=3)
    ap.add_argument("--output", required=True)
    ap.add_argument("--summary-out", default=None, help="額外輸出一份 Markdown 修補摘要（供 PR 描述使用）")
    args = ap.parse_args()

    workspace = Path(args.workspace)
    repo_dir = Path(args.repo_dir)

    start_rounds, attempts = load_history(workspace)
    reconstruct_from_logs(workspace, attempts, args.max_build_attempts)

    if not attempts:
        raise SystemExit("找不到任何 round/attempt 記錄（.cve-loop-history.jsonl 與 pipeline-logs 都是空的），沒有東西可以產報表。")

    rounds_rows = build_rounds_table(start_rounds, attempts, repo_dir)
    cve_rows, first_snap, last_snap = build_cve_tracking(rounds_rows, workspace)

    # --- 總覽數字 ---
    ok_rows = [r for r in rounds_rows if r.get("result") == "BUILD_OK"]
    failed_rows = [r for r in rounds_rows if r.get("result") in ("BUILD_FAILED", "BUILD_FAILED_GIVE_UP")]

    start_fixable = first_snap[1] if first_snap else None
    start_fixable_n = sum(1 for v in start_fixable.values() if v["fixed_versions"]) if start_fixable is not None else None
    final_snapshot = last_snap[1] if last_snap else None
    final_fixable_n = sum(1 for v in final_snapshot.values() if v["fixed_versions"]) if final_snapshot is not None else None
    final_unfixable_n = sum(1 for v in final_snapshot.values() if not v["fixed_versions"]) if final_snapshot is not None else None

    fixed_count = None
    fix_rate = "-"
    if start_fixable_n is not None and final_fixable_n is not None:
        fixed_count = start_fixable_n - final_fixable_n
        if start_fixable_n > 0:
            fix_rate = f"{fixed_count / start_fixable_n * 100:.1f}%"

    all_severities = set()
    for snap in ([first_snap[1]] if first_snap else []) + ([last_snap[1]] if last_snap else []):
        for v in snap.values():
            all_severities.add(v["severity"])
    severities_sorted = sorted(
        all_severities,
        key=lambda s: SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else len(SEVERITY_ORDER),
    )

    ts_values = [parse_ts(r.get("ts_start")) or parse_ts(r.get("ts_end")) for r in rounds_rows]
    ts_values = [t for t in ts_values if t is not None]
    ts_end_values = [parse_ts(r.get("ts_end")) for r in rounds_rows]
    ts_end_values = [t for t in ts_end_values if t is not None]

    start_time = fmt_ts(min(ts_values)) if ts_values else "-"
    if ts_values and ts_end_values:
        total_duration = fmt_duration((max(ts_end_values) - min(ts_values)).total_seconds())
    else:
        total_duration = "-"

    rounds_executed = max(r["outer_round"] for r in rounds_rows)
    stopped_reason = None
    if rounds_executed >= args.max_rounds and (final_fixable_n is None or final_fixable_n > 0):
        stopped_reason = f"已達輪數上限（{args.max_rounds} 輪），流程停止"
    elif final_fixable_n == 0:
        stopped_reason = "所有可修復 CVE 皆已修復，流程完成"

    meta = {
        "start_time": start_time,
        "total_duration": total_duration,
        "severities": ", ".join(severities_sorted) if severities_sorted else "-",
        "nightly_branch": args.nightly_branch,
        "last_good_branch": args.last_good_branch,
        "start_fixable": start_fixable_n if start_fixable_n is not None else "-",
        "final_fixable": final_fixable_n if final_fixable_n is not None else "-",
        "fixed_count": fixed_count if fixed_count is not None else "-",
        "fix_rate": fix_rate,
        "final_unfixable": final_unfixable_n if final_unfixable_n is not None else "-",
        "max_rounds": args.max_rounds,
        "rounds_executed": rounds_executed,
        "max_build_attempts": args.max_build_attempts,
        "success_count": len(ok_rows),
        "failed_count": len(failed_rows),
        "stopped_reason": stopped_reason,
    }

    wb = Workbook()
    build_sheet1(wb, meta)
    build_sheet2(wb, rounds_rows)
    build_sheet3(wb, cve_rows)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"報表已產出: {out_path}")

    if args.summary_out:
        summary_lines = [
            "## CVE 自動化修補摘要",
            "",
            f"- 執行時間：{meta['start_time']}，總耗時 {meta['total_duration']}",
            f"- 分支：`{meta['nightly_branch']}` → `{meta['last_good_branch']}`",
            f"- 涉及嚴重度：{meta['severities']}",
            f"- 初始可修復 CVE：{meta['start_fixable']}，最終可修復 CVE：{meta['final_fixable']}"
            f"（已修復 {meta['fixed_count']} 個，修復率 {meta['fix_rate']}）",
            f"- 仍不可修復 CVE：{meta['final_unfixable']}",
            f"- 執行輪數：{meta['rounds_executed']} / {meta['max_rounds']}"
            f"（每輪最多 {meta['max_build_attempts']} 次建置嘗試）",
            f"- 建置成功 {meta['success_count']} 次、失敗 {meta['failed_count']} 次",
            f"- 停止原因：{meta['stopped_reason'] or '-'}",
            "",
            "完整逐輪嘗試紀錄與 CVE 追蹤清單請見附件 Excel 報表。",
        ]
        summary_path = Path(args.summary_out)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        print(f"PR 摘要已產出: {summary_path}")


if __name__ == "__main__":
    main()
