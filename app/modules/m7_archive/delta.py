from collections import Counter

from app.codec import canonical

TABLES = ("roads", "points", "windows")
FIELDS = ("responsibility_m", "covered_m", "pending_m", "unknown_m", "difference_m", "duplicate_m")


def merge_sources(base, supplement):
    """把补扫行按内容去重后追加到基线冻结输入，同一补扫重跑或续跑不产生新行。"""
    merged = {"batch": base["batch"], "at": base["at"]}
    added = {}
    for table in TABLES:
        seen = {canonical(row) for row in base[table]}
        rows = []
        for row in supplement[table]:
            key = canonical(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        merged[table] = base[table] + rows
        added[table] = len(rows)
    return merged, added


def _issue_key(issue):
    return canonical({key: issue.get(key) for key in ("table", "line", "end_line", "code")})


def new_issues(base_issues, amended_issues):
    """修订相对基线新增的判定记录；有新增即说明补扫未与线路登记核清。"""
    remaining = Counter(_issue_key(item) for item in base_issues)
    fresh = []
    for item in amended_issues:
        key = _issue_key(item)
        if remaining[key]:
            remaining[key] -= 1
        else:
            fresh.append(item)
    return fresh


def _lane_key(row):
    return row["road"], row["direction"]


def _fields(row):
    return {field: (row or {}).get(field, 0) for field in FIELDS}


def lane_deltas(base_rows, amended_rows):
    base = {_lane_key(row): row for row in base_rows}
    amended = {_lane_key(row): row for row in amended_rows}
    lanes = []
    for key in sorted(set(base) | set(amended)):
        before, after = _fields(base.get(key)), _fields(amended.get(key))
        delta = {field: after[field] - before[field] for field in FIELDS}
        if any(delta.values()):
            lanes.append({"road": key[0], "direction": key[1],
                          "name": (amended.get(key) or base.get(key))["name"],
                          "base": before, "amended": after, "delta": delta})
    return lanes


def _kind_at(parts, meter):
    for part in parts:
        if part["from_m"] <= meter < part["to_m"]:
            return part["kind"]
    return None


def _references_at(parts, meter):
    for part in parts:
        if part["from_m"] <= meter < part["to_m"]:
            return [{"vehicle": ref["vehicle"], "line": ref["line"], "end_line": ref["end_line"],
                     "window_lines": ref["window_lines"]} for ref in part["references"]]
    return []


def interval_changes(base_rows, amended_rows):
    """逐方向找出判定类别发生变化的里程区间，并挂上修订后采信的原始行。"""
    base = {_lane_key(row): row["parts"] for row in base_rows}
    amended = {_lane_key(row): row["parts"] for row in amended_rows}
    changes = []
    for key in sorted(set(base) | set(amended)):
        before, after = base.get(key, []), amended.get(key, [])
        meters = sorted({part["from_m"] for part in before + after}
                        | {part["to_m"] for part in before + after})
        for low, high in zip(meters, meters[1:]):
            old, new = _kind_at(before, low), _kind_at(after, low)
            if old == new:
                continue
            references = _references_at(after, low)
            last = changes[-1] if changes else None
            if (last and last["road"] == key[0] and last["direction"] == key[1]
                    and last["to_m"] == low and last["from"] == old and last["to"] == new
                    and last["references"] == references):
                last["to_m"] = high
                last["m"] = high - last["from_m"]
            else:
                changes.append({"road": key[0], "direction": key[1], "from_m": low, "to_m": high,
                                "m": high - low, "from": old, "to": new, "references": references})
    return changes


def _supplement_lines(base_counts, added):
    return {table: list(range(base_counts[table] + 1, base_counts[table] + added[table] + 1))
            for table in TABLES if added[table]}


def build_delta(base, amended_result, amendment):
    """相对已签收基线的交班差额：里程变化、区间变化、所用原始行与核清状态。"""
    base_result = base["result"]
    fresh = new_issues(base_result["reconcile"]["issues"], amended_result["reconcile"]["issues"])
    base_totals = base_result["reconcile"]["totals"]
    amended_totals = amended_result["reconcile"]["totals"]
    return {"base": {"id": amendment["base"], "digest": base["manifest"]["digest"],
                     "reviewer": (base.get("signature") or {}).get("reviewer")},
            "verified": not fresh,
            "status": amended_result["reconcile"]["status"],
            "issues": fresh,
            "totals": {field: {"base": base_totals[field], "amended": amended_totals[field],
                               "delta": amended_totals[field] - base_totals[field]}
                       for field in FIELDS},
            "lanes": lane_deltas(base_result["reconcile"]["rows"], amended_result["reconcile"]["rows"]),
            "changes": interval_changes(base_result["coverage"]["rows"],
                                        amended_result["coverage"]["rows"]),
            "supplement": {"batch": amendment["batch"], "at": amendment["at"],
                           "added": amendment["added"],
                           "lines": _supplement_lines(base["manifest"]["input_counts"],
                                                      amendment["added"])}}
