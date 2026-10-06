"""争议日志隔离（复核员批量隔离/恢复）HTTP 集成测试。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)
    from app import database  # noqa: F401
    from app.main import app

    with TestClient(app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _log(no, station, counterpart, t, band="40m", mode="SSB"):
    return {
        "record_no": no,
        "station": station,
        "counterpart": counterpart,
        "time": t,
        "band": band,
        "mode": mode,
    }


T0 = "2026-10-04T00:00:00+00:00"


def _seed_six(client, drill="Q", allowed=50):
    """3 对可配对记录：(A1,A2) t0/t5、(B1,B2) t0/t8、(C1,C2) t0/t9。"""
    client.post(
        "/drills",
        json={"drill_id": drill, "allowed_time_diff_seconds": allowed},
    )
    client.post(f"/drills/{drill}/logs", json={"logs": [
        _log("A1", "S1", "S2", T0),
        _log("A2", "S2", "S1", "2026-10-04T00:00:05+00:00"),
        _log("B1", "S3", "S4", T0),
        _log("B2", "S4", "S3", "2026-10-04T00:00:08+00:00"),
        _log("C1", "S5", "S6", T0),
        _log("C2", "S6", "S5", "2026-10-04T00:00:09+00:00"),
    ]})


# --------------------------------------------------------------------------
# 隔离：配对资格 / 未配对清单 / 版本 / 清单排序
# --------------------------------------------------------------------------
def test_quarantine_removes_pair_but_keeps_log(client):
    _seed_six(client)
    before = client.get("/drills/Q/result").json()
    assert len(before["pairs"]) == 3 and before["version"] == 1

    r = client.put("/drills/Q/quarantines", json={"record_nos": ["A1", "B2"]})
    assert r.status_code == 200, r.text
    body = r.json()
    # 清单按记录号升序
    assert [q["record_no"] for q in body["quarantines"]] == ["A1", "B2"]
    assert "quarantined_at" in body["quarantines"][0]

    pairs = sorted((p["record_a"], p["record_b"]) for p in body["result"]["pairs"])
    # (A1,A2) 与 (B1,B2) 因一端隔离而退出；只剩 (C1,C2)
    assert pairs == [("C1", "C2")]
    # 隔离记录不进入未配对清单；未配对只剩 A2、B1
    assert sorted(body["result"]["unpaired"]) == ["A2", "B1"]
    assert body["result"]["version"] == before["version"] + 1

    # 日志仍在、原字段不改写，并带 quarantined 标记
    logs = {l["record_no"]: l for l in client.get("/drills/Q/logs").json()}
    assert len(logs) == 6
    assert logs["A1"]["quarantined"] is True
    assert logs["C1"]["quarantined"] is False
    assert logs["A1"]["station"] == "S1"


def test_quarantine_list_sorted_and_sealed_readable(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["C1", "A2"]})
    r = client.get("/drills/Q/quarantines")
    assert r.status_code == 200
    body = r.json()
    assert body["drill_id"] == "Q"
    assert [q["record_no"] for q in body["quarantines"]] == ["A2", "C1"]
    assert body["result"]["drill_id"] == "Q"

    # 封存后仍可查清单
    client.post("/drills/Q/seal")
    assert client.get("/drills/Q/quarantines").status_code == 200


def test_quarantine_repeated_is_idempotent_no_version_bump(client):
    _seed_six(client)
    b1 = client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]}).json()
    # 重复隔离已隔离记录（混一条新增）：仅新增推进；之后纯重传不推进。
    b2 = client.put("/drills/Q/quarantines", json={"record_nos": ["A1", "B2"]}).json()
    assert b2["result"]["version"] == b1["result"]["version"] + 1
    assert [q["record_no"] for q in b2["quarantines"]] == ["A1", "B2"]

    b3 = client.put("/drills/Q/quarantines", json={"record_nos": ["B2", "A1"]}).json()
    assert b3["result"]["version"] == b2["result"]["version"]
    assert [q["record_no"] for q in b3["quarantines"]] == ["A1", "B2"]


def test_quarantine_unknown_drill_is_404(client):
    r = client.put("/drills/NOPE/quarantines", json={"record_nos": ["x"]})
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"
    r = client.get("/drills/NOPE/quarantines")
    assert r.status_code == 404 and r.json()["error"] == "drill_not_found"


def test_quarantine_batch_validation(client):
    _seed_six(client)

    # 记录不存在 -> 整批拒绝
    r = client.put("/drills/Q/quarantines", json={"record_nos": ["A1", "NOPE"]})
    assert r.status_code == 404 and r.json()["error"] == "quarantine_record_missing"

    # 批次内重复 -> 整批拒绝
    r = client.put("/drills/Q/quarantines", json={"record_nos": ["A1", "A1"]})
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_quarantine_batch"

    # 空列表 -> 422
    r = client.put("/drills/Q/quarantines", json={"record_nos": []})
    assert r.status_code == 422

    # 整批拒绝状态不变
    assert client.get("/drills/Q/quarantines").json()["quarantines"] == []
    assert client.get("/drills/Q/result").json()["version"] == 1


def test_quarantine_locked_member_rejected_whole_batch(client):
    _seed_six(client)
    client.put("/drills/Q/locks", json={"record_a": "A1", "record_b": "A2"})
    v = client.get("/drills/Q/result").json()["version"]

    # 批次中含锁定成员 A1 -> 整批拒绝（即使 A1 已在前）
    r = client.put("/drills/Q/quarantines", json={"record_nos": ["B2", "A1"]})
    assert r.status_code == 409 and r.json()["error"] == "quarantine_record_locked"
    assert client.get("/drills/Q/result").json()["version"] == v
    assert client.get("/drills/Q/quarantines").json()["quarantines"] == []

    # 单独隔离 B2（非锁定成员）可行
    r = client.put("/drills/Q/quarantines", json={"record_nos": ["B2"]})
    assert r.status_code == 200


def test_lock_quarantined_record_rejected(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})

    r = client.put("/drills/Q/locks", json={"record_a": "A1", "record_b": "A2"})
    assert r.status_code == 409 and r.json()["error"] == "record_quarantined"
    # 版本未因失败锁定而变（隔离推进过一次到 2）
    assert client.get("/drills/Q/result").json()["version"] == 2
    assert client.get("/drills/Q/locks").json()["locks"] == []


def test_sealed_drill_rejects_quarantine_actions(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})
    client.post("/drills/Q/seal")

    r = client.put("/drills/Q/quarantines", json={"record_nos": ["B2"]})
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["A1"]})
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"
    # 状态不变
    got = client.get("/drills/Q/quarantines").json()
    assert [q["record_no"] for q in got["quarantines"]] == ["A1"]


# --------------------------------------------------------------------------
# 恢复：按当前日志重新求解、幂等、整批拒绝
# --------------------------------------------------------------------------
def test_restore_recomputes_with_current_logs(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})
    v = client.get("/drills/Q/result").json()["version"]

    # 隔离期间更正 A2（仍与 A1 互指，时差变为 6s）——隔离状态保留
    r = client.put(
        "/drills/Q/logs/A2",
        json=_log("A2", "S2", "S1", "2026-10-04T00:00:06+00:00"),
    )
    assert r.status_code == 200
    # 更正推进版本，但 A1 仍隔离：不配对、不在未配对清单
    cur = client.get("/drills/Q/result").json()
    assert all(p != {"record_a": "A1", "record_b": "A2",
                     "time_diff_seconds": 6.0} for p in cur["pairs"])
    assert "A1" not in cur["unpaired"]

    # 恢复 A1 -> 按当前日志（6s 时差）重新配对
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["A1"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quarantines"] == []
    pair_map = {(p["record_a"], p["record_b"]): p["time_diff_seconds"]
                for p in body["result"]["pairs"]}
    assert pair_map[("A1", "A2")] == 6.0
    assert body["result"]["unpaired"] == []
    assert body["result"]["version"] == cur["version"] + 1


def test_restore_idempotent_and_batch_validation(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1", "B2"]})

    # 恢复一条已隔离 + 一条当前未隔离（A2）：仅 A1 实际恢复；幂等部分不报错
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["A1", "A2"]})
    assert r.status_code == 200
    assert [q["record_no"] for q in r.json()["quarantines"]] == ["B2"]

    # 全部恢复后再恢复任意现存记录（含从未隔离的 C1）：幂等，不推进版本
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["B2"]})
    assert r.status_code == 200
    v_after = r.json()["result"]["version"]
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["B2", "C1"]})
    assert r.status_code == 200
    assert r.json()["result"]["version"] == v_after

    # 记录不存在 -> 整批拒绝
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["NOPE"]})
    assert r.status_code == 404 and r.json()["error"] == "restore_record_missing"

    # 批次内重复 -> 整批拒绝
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": ["A1", "A1"]})
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_quarantine_batch"

    # 空列表 -> 422
    r = client.post("/drills/Q/quarantines/restore",
                    json={"record_nos": []})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# 与其他只读/写入能力的联动
# --------------------------------------------------------------------------
def test_quarantine_excluded_from_gap_diagnostics(client):
    # X1-X2 互指但时差超限：正常时 X1/X2 互为 time_diff_exceeded 缺口
    client.post("/drills", json={"drill_id": "G",
                                 "allowed_time_diff_seconds": 10})
    client.post("/drills/G/logs", json={"logs": [
        _log("X1", "T1", "T2", T0),
        _log("X2", "T2", "T1", "2026-10-04T00:00:20+00:00"),
    ]})
    gaps = client.get("/drills/G/gap-diagnostics").json()["gaps"]
    assert {g["record_no"] for g in gaps} == {"X1", "X2"}

    # 隔离 X1：X1 不再出现在缺口诊断；X2 的候选也仅限未隔离日志 -> 无互指
    client.put("/drills/G/quarantines", json={"record_nos": ["X1"]})
    body = client.get("/drills/G/gap-diagnostics").json()
    nos = {g["record_no"]: g for g in body["gaps"]}
    assert "X1" not in nos
    assert nos["X2"]["reason"] == "missing_counterpart_log"
    assert nos["X2"]["counterpart_record_no"] is None


def test_quarantine_excluded_from_station_summary(client):
    _seed_six(client)
    # 隔离 A1（台站 S1）与 B2（台站 S4）
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1", "B2"]})
    body = client.get("/drills/Q/station-summary").json()
    stations = {s["station"]: s for s in body["stations"]}

    # S1 只剩 0 条（A1 是其唯一日志且被隔离）-> 不产生汇总项
    assert "S1" not in stations
    # S2：A2 未隔离但未配对
    assert stations["S2"]["total_logs"] == 1
    assert stations["S2"]["paired_logs"] == 0
    assert stations["S2"]["unpaired_logs"] == 1
    # S4：B2 被隔离后无日志项
    assert "S4" not in stations
    # S5/S6 照常配对
    assert stations["S5"]["paired_logs"] == 1
    assert stations["S5"]["paired_coverage"] == 1.0


def test_quarantine_correction_preserves_state(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})

    # 隔离期间按原接口更正 A1：可写、隔离状态保留、不参与配对
    r = client.put(
        "/drills/Q/logs/A1",
        json=_log("A1", "S1", "S2", "2026-10-04T00:00:03+00:00"),
    )
    assert r.status_code == 200
    logs = {l["record_no"]: l for l in client.get("/drills/Q/logs").json()}
    assert logs["A1"]["quarantined"] is True
    pairs = r.json()["pairs"]
    assert all("A1" not in (p["record_a"], p["record_b"]) for p in pairs)
    assert "A1" not in r.json()["unpaired"]

    # 隔离清单仍含 A1
    assert [q["record_no"] for q in
            client.get("/drills/Q/quarantines").json()["quarantines"]] == ["A1"]


def test_quarantine_blocks_transfer(client):
    # 建源/目标两个演练
    client.post("/drills", json={"drill_id": "SRC",
                                 "allowed_time_diff_seconds": 10})
    client.post("/drills", json={"drill_id": "DST",
                                 "allowed_time_diff_seconds": 30})
    client.put("/drills/SRC/logs/A1",
               json=_log("A1", "S1", "S2", T0))
    client.put("/drills/SRC/logs/A2",
               json=_log("A2", "S2", "S1", "2026-10-04T00:00:05+00:00"))

    client.put("/drills/SRC/quarantines", json={"record_nos": ["A1"]})
    r = client.post("/drills/SRC/transfer",
                    json={"target_drill_id": "DST", "record_nos": ["A1", "A2"]})
    assert r.status_code == 409
    assert r.json()["error"] == "transfer_record_quarantined"

    # 两端状态不变（源：两次单条写入 + 一次隔离 = 版本 3；目标仍为 0）
    assert len(client.get("/drills/SRC/logs").json()) == 2
    assert client.get("/drills/DST/logs").json() == []
    assert client.get("/drills/SRC/result").json()["version"] == 3
    assert client.get("/drills/DST/result").json()["version"] == 0


# --------------------------------------------------------------------------
# 快照：同事务留存、历史不变、差异含隔离节
# --------------------------------------------------------------------------
def test_quarantine_snapshot_written_same_transaction(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})
    client.post("/drills/Q/quarantines/restore", json={"record_nos": ["A1"]})

    versions = client.get("/drills/Q/snapshots").json()["versions"]
    # v0 创建、v1 写入、v2 隔离、v3 恢复
    assert versions == [0, 1, 2, 3]

    snap2 = client.get("/drills/Q/snapshots/2").json()
    assert [q["record_no"] for q in snap2["quarantines"]] == ["A1"]
    pairs2 = {(p["record_a"], p["record_b"]) for p in snap2["pairs"]}
    assert ("A1", "A2") not in pairs2

    snap3 = client.get("/drills/Q/snapshots/3").json()
    assert snap3["quarantines"] == []
    pairs3 = {(p["record_a"], p["record_b"]) for p in snap3["pairs"]}
    assert ("A1", "A2") in pairs3

    # 历史快照（v2）保持原貌
    assert [q["record_no"] for q in
            client.get("/drills/Q/snapshots/2").json()["quarantines"]] == ["A1"]


def test_snapshot_diff_includes_quarantines(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})
    cur = client.get("/drills/Q/result").json()["version"]
    d = client.get(f"/drills/Q/snapshots/{cur - 1}/diff/{cur}").json()
    assert [q["record_no"] for q in d["quarantines"]["added"]] == ["A1"]
    assert d["quarantines"]["removed"] == []

    client.post("/drills/Q/quarantines/restore", json={"record_nos": ["A1"]})
    cur2 = client.get("/drills/Q/result").json()["version"]
    d = client.get(f"/drills/Q/snapshots/{cur}/diff/{cur2}").json()
    assert d["quarantines"]["added"] == []
    assert [q["record_no"] for q in d["quarantines"]["removed"]] == ["A1"]

    # 相同版本：隔离差异节也为空
    d = client.get(f"/drills/Q/snapshots/{cur2}/diff/{cur2}").json()
    assert d["quarantines"] == {"added": [], "removed": []}


# --------------------------------------------------------------------------
# 批量预演/确认遵守隔离状态
# --------------------------------------------------------------------------
def test_preview_respects_quarantine(client):
    _seed_six(client)
    client.put("/drills/Q/quarantines", json={"record_nos": ["A1"]})
    base = client.get("/drills/Q/result").json()["version"]

    # 预演更正隔离记录 A1：仍不具互证资格，拟写入结果中 A1 不配对
    r = client.post("/drills/Q/logs/preview", json={"logs": [
        _log("A1", "S1", "S2", "2026-10-04T00:00:03+00:00"),
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_version"] == base
    assert body["would_advance_version"] is True
    pairs = body["proposed_result"]["pairs"]
    assert all("A1" not in (p["record_a"], p["record_b"]) for p in pairs)
    assert "A1" not in body["proposed_result"]["unpaired"]
    # 预演不改状态
    assert client.get("/drills/Q/result").json()["version"] == base

    # 确认后同样遵守隔离
    r = client.post("/drills/Q/logs/confirm", json={
        "logs": [_log("A1", "S1", "S2", "2026-10-04T00:00:03+00:00")],
        "base_version": body["base_version"],
        "token": body["token"],
    })
    assert r.status_code == 200, r.text
    assert all("A1" not in (p["record_a"], p["record_b"])
               for p in r.json()["pairs"])
    # 隔离状态仍保留
    assert [q["record_no"] for q in
            client.get("/drills/Q/quarantines").json()["quarantines"]] == ["A1"]


def test_confirm_rejected_after_quarantine_change(client):
    _seed_six(client)
    pv = client.post("/drills/Q/logs/preview", json={"logs": [
        _log("A1", "S1", "S2", "2026-10-04T00:00:03+00:00"),
    ]}).json()
    base = pv["base_version"]

    # 预演后发生隔离（推进版本）-> 确认时基准版本已变
    client.put("/drills/Q/quarantines", json={"record_nos": ["C1"]})
    r = client.post("/drills/Q/logs/confirm", json={
        "logs": [_log("A1", "S1", "S2", "2026-10-04T00:00:03+00:00")],
        "base_version": base,
        "token": pv["token"],
    })
    assert r.status_code == 409
    assert r.json()["error"] == "preview_base_version_changed"
    # 状态不变
    assert client.get("/drills/Q/result").json()["version"] == base + 1


# --------------------------------------------------------------------------
# 旧库升级：快照表补列，历史快照可读（隔离清单为空）
# --------------------------------------------------------------------------
def test_legacy_snapshots_table_migration(tmp_path, monkeypatch):
    db = str(tmp_path / "legacy.db")
    monkeypatch.setenv("QSO_DB_PATH", db)
    import sqlite3

    from app import database

    # 手工建一张“旧版”库（snapshots 无 quarantines_json 列）并填一行快照
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE drills (
            drill_id TEXT PRIMARY KEY, allowed_diff_secs REAL NOT NULL
            CHECK (allowed_diff_secs >= 0), version INTEGER NOT NULL DEFAULT 0,
            sealed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            sealed_at TEXT);
        CREATE TABLE logs (
            drill_id TEXT NOT NULL, record_no TEXT NOT NULL, station TEXT NOT NULL,
            counterpart TEXT NOT NULL, ts_micros INTEGER NOT NULL,
            ts_iso TEXT NOT NULL, band TEXT NOT NULL, mode TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (drill_id, record_no));
        CREATE TABLE results (
            drill_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
            pairs_json TEXT NOT NULL, unpaired_json TEXT NOT NULL,
            total_time_diff_seconds REAL NOT NULL, recomputed_at TEXT NOT NULL);
        CREATE TABLE locks (
            drill_id TEXT NOT NULL, record_a TEXT NOT NULL, record_b TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY (drill_id, record_a, record_b));
        CREATE TABLE quarantines (
            drill_id TEXT NOT NULL, record_no TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (drill_id, record_no));
        CREATE TABLE snapshots (
            drill_id TEXT NOT NULL, version INTEGER NOT NULL, pairs_json TEXT NOT NULL,
            unpaired_json TEXT NOT NULL, total_time_diff_seconds REAL NOT NULL,
            locks_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (drill_id, version));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO drills VALUES ('OLD', 10, 5, 0, 't0', NULL)"
    )
    conn.execute(
        "INSERT INTO results VALUES ('OLD', 5, '[]', '[]', 0, 't0')"
    )
    conn.execute(
        "INSERT INTO snapshots VALUES ('OLD', 5, '[]', '[]', 0, '[]', 't0')"
    )
    conn.commit()
    conn.close()

    database.init_db()  # 触发补列迁移（该演练已有快照，不被回填重复处理）

    # 旧快照仍可读取，隔离节为空列表（历史原貌，未被改写）
    snap = database.get_snapshot("OLD", 5)
    assert snap["version"] == 5 and snap["quarantines"] == []
    # 新版本快照写入带隔离列
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    assert "quarantines_json" in cols
    conn.close()
