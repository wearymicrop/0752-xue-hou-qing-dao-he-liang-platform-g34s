import pytest


def supplement(points, windows=(), roads=()):
    return {"batch": "雪勤-0117-补扫", "at": "2026-01-17T06:40:00+08:00",
            "roads": list(roads), "points": list(points), "windows": list(windows)}


VERIFIED_POINTS = [
    {"vehicle": "云扫-11", "road": "S01", "direction": "+", "time": "2026-01-17T06:10:00+08:00",
     "plow": True, "x": 780, "y": 0},
    {"vehicle": "云扫-11", "road": "S01", "direction": "+", "time": "2026-01-17T06:11:30+08:00",
     "plow": True, "x": 950, "y": 0},
    {"vehicle": "云扫-11", "road": "S01", "direction": "+", "time": "2026-01-17T06:12:40+08:00",
     "plow": False, "x": 1000, "y": 0},
]

UNVERIFIED_POINTS = [
    {"vehicle": "云扫-12", "road": "S05", "direction": "+", "time": "2026-01-17T06:10:00+08:00",
     "plow": True, "x": 610, "y": 0},
    {"vehicle": "云扫-12", "road": "S05", "direction": "+", "time": "2026-01-17T06:11:30+08:00",
     "plow": True, "x": 820, "y": 120},
    {"vehicle": "云扫-12", "road": "S99", "direction": "+", "time": "2026-01-17T06:12:00+08:00",
     "plow": True, "x": 0, "y": 0},
]

UNVERIFIED_WINDOWS = [
    {"road": "S05", "direction": "+", "start": "2026-01-17T06:00:00+08:00",
     "end": "2026-01-17T06:16:00+08:00"},
    {"road": "S05", "direction": "-", "start": "2026-01-17T06:00:00+08:00",
     "end": "2026-01-17T06:16:00+08:00"},
]


def signed_base(client, dirty):
    run = client.post("/api/runs", json={"source": dirty}).json()
    receipt = client.post(f"/api/runs/{run['id']}/sign",
                          json={"reviewer": "周洁", "digest": run["manifest"]["digest"]})
    assert receipt.status_code == 200, receipt.text
    return run


def amend(client, ident, source):
    response = client.post(f"/api/runs/{ident}/amend", json={"source": source})
    assert response.status_code == 200, response.text
    return response.json()


def lane(run, road, direction):
    for row in run["result"]["reconcile"]["rows"]:
        if row["road"] == road and row["direction"] == direction:
            return row
    raise AssertionError(f"找不到方向 {road}{direction}")


def test_amend_requires_signed_base(client, dirty):
    run = client.post("/api/runs", json={"source": dirty}).json()
    response = client.post(f"/api/runs/{run['id']}/amend", json={"source": supplement(VERIFIED_POINTS)})
    assert response.status_code == 409 and response.json()["code"] == "BASE_UNSIGNED"


def test_amend_rejects_bad_supplement(client, dirty):
    base = signed_base(client, dirty)
    response = client.post(f"/api/runs/{base['id']}/amend", json={"source": {"batch": "补扫"}})
    assert response.status_code == 422 and response.json()["code"] == "BAD_SCHEMA"
    assert len(client.get("/api/runs").json()["runs"]) == 1


def test_verified_supplement_moves_whole_chain(client, dirty):
    base = signed_base(client, dirty)
    run = amend(client, base["id"], supplement(VERIFIED_POINTS))
    assert not run["reused"] and run["id"] != base["id"]
    amendment = run["manifest"]["amendment"]
    assert amendment["base"] == base["id"] and amendment["verified"] and amendment["added"]["points"] == 3

    after = lane(run, "S01", "+")
    assert (after["covered_m"], after["pending_m"], after["unknown_m"]) == (1000, 0, 0)
    assert run["result"]["reconcile"]["status"] == "MATCHED"
    windows = [row for row in run["result"]["windows"]["rows"] if row["vehicle"] == "云扫-11"]
    assert [(row["from_m"], row["to_m"], row["kind"]) for row in windows] == [(780, 950, "covered"), (950, 1000, "covered")]

    delta = run["delta"]
    assert delta["verified"] and delta["issues"] == [] and delta["base"]["reviewer"] == "周洁"
    assert delta["supplement"]["lines"] == {"points": [42, 43, 44]}
    (change,) = delta["lanes"]
    assert (change["road"], change["direction"]) == ("S01", "+")
    assert change["delta"]["covered_m"] == 220 and change["delta"]["pending_m"] == -50
    assert change["delta"]["unknown_m"] == -170 and change["delta"]["responsibility_m"] == 0
    spans = [(row["from_m"], row["to_m"], row["from"], row["to"]) for row in delta["changes"]]
    assert spans == [(780, 950, "unknown", "covered"), (950, 1000, "pending", "covered")]
    assert all(any(ref["vehicle"] == "云扫-11" for ref in row["references"]) for row in delta["changes"])


def test_unverified_supplement_stays_draft(client, dirty):
    base = signed_base(client, dirty)
    run = amend(client, base["id"], supplement(UNVERIFIED_POINTS, UNVERIFIED_WINDOWS))
    delta = run["delta"]
    assert not delta["verified"] and not run["manifest"]["amendment"]["verified"]
    assert {row["code"] for row in delta["issues"]} >= {"UNKNOWN_LANE", "WINDOW_SCHEMA"}
    (change,) = delta["lanes"]
    assert (change["road"], change["delta"]["covered_m"]) == ("S05", 250)  # 里程只进草稿差额

    response = client.post(f"/api/runs/{run['id']}/sign",
                           json={"reviewer": "李颖", "digest": run["manifest"]["digest"]})
    assert response.status_code == 409 and response.json()["code"] == "SUPPLEMENT_UNVERIFIED"
    assert client.get(f"/api/runs/{run['id']}").json()["signature"] is None
    assert lane(run, "S05", "+")["covered_m"] == 250  # 草稿差额可查但不构成正式结论


def test_verified_amendment_signs_and_base_voucher_survives(client, dirty):
    base = signed_base(client, dirty)
    run = amend(client, base["id"], supplement(VERIFIED_POINTS))
    receipt = client.post(f"/api/runs/{run['id']}/sign",
                          json={"reviewer": "李颖", "digest": run["manifest"]["digest"]})
    assert receipt.status_code == 200 and receipt.json()["reviewer"] == "李颖"

    reopened = client.get(f"/api/runs/{base['id']}").json()
    assert reopened["signature"]["reviewer"] == "周洁"
    assert reopened["manifest"]["digest"] == base["manifest"]["digest"]
    assert lane(reopened, "S01", "+")["covered_m"] == 780  # 旧签收结论保持原样
    assert {item["id"] for item in client.get("/api/runs").json()["runs"]} == {base["id"], run["id"]}


def test_amend_replay_is_idempotent(client, dirty):
    base = signed_base(client, dirty)
    first = amend(client, base["id"], supplement(VERIFIED_POINTS))
    second = amend(client, base["id"], supplement(VERIFIED_POINTS))
    assert second["reused"] and second["id"] == first["id"]
    assert lane(second, "S01", "+")["covered_m"] == 1000  # 重跑不重复叠加覆盖
    assert len(client.get("/api/runs").json()["runs"]) == 2


def test_amend_resume_and_retransmission_never_double_count(client, dirty):
    base = signed_base(client, dirty)
    partial = amend(client, base["id"], supplement(VERIFIED_POINTS[:2]))
    assert lane(partial, "S01", "+")["covered_m"] == 950

    resumed = amend(client, base["id"], supplement(VERIFIED_POINTS))  # 断点续跑：累计补扫并入同一基线
    assert resumed["id"] != partial["id"] and lane(resumed, "S01", "+")["covered_m"] == 1000

    again = amend(client, base["id"], supplement(VERIFIED_POINTS[:2]))
    assert again["reused"] and again["id"] == partial["id"]

    echo = amend(client, base["id"], supplement(dirty["points"][:2]))  # 重传基线已有行
    assert echo["reused"] and echo["id"] == base["id"]
    assert len(client.get("/api/runs").json()["runs"]) == 3


def test_delta_endpoint_reproduces_handover(client, dirty):
    base = signed_base(client, dirty)
    run = amend(client, base["id"], supplement(VERIFIED_POINTS))
    delta = client.get(f"/api/runs/{run['id']}/delta").json()
    assert delta == run["delta"]

    response = client.get(f"/api/runs/{base['id']}/delta")
    assert response.status_code == 422 and response.json()["code"] == "NOT_AMENDMENT"
