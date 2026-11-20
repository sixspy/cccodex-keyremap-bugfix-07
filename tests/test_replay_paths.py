
import sqlite3

from fastapi.testclient import TestClient

from app import app, init_db

import pytest


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr("app.DB_PATH", db)
    init_db()
    yield db


def _client():
    return TestClient(app)


def _auth(c):
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + token}


def _raw(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _conflict_id(c, h, old_key="OLD-C9"):
    rows = c.get("/api/conflicts", headers=h).json()["items"]
    return next(x["id"] for x in rows if x["old_key"] == old_key)


def _add_mapping(db, old_key, new_key, kind="一对一"):
    with _raw(db) as raw:
        ver = raw.execute("SELECT version FROM map_versions WHERE status='生效'").fetchone()["version"]
        raw.execute("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
                    (ver, old_key, new_key, kind))


# 甲路径：目标已在正式且内容一致 -> 幂等跳过，正式零改，只写 replay_skip，generation 不抬
def test_replay_skip_path_is_idempotent_formal_untouched(_db):
    c = _client(); h = _auth(c)
    _add_mapping(_db, "OLD-C9", "NEW-100")  # 正式区已有 NEW-100/OLD-A1，先造一次不同来源的冲突重放
    cid = _conflict_id(c, h)

    first = c.post(f"/api/conflicts/{cid}/replay", headers=h).json()
    assert first["path"] == "overwrite" and first["generation"] == 2

    # 同一冲突已置「已重放」；再造一条同键冲突，重放结果与正式现态一致，走甲路径
    with _raw(_db) as raw:
        raw.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
                    ("OLD-C9", "无映射", "挂起"))
    cid2 = _conflict_id(c, h)
    r = c.post(f"/api/conflicts/{cid2}/replay", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "skip" and body["generation"] == 2

    with _raw(_db) as raw:
        formal = raw.execute("SELECT * FROM formal_entities WHERE entity_key='NEW-100'").fetchone()
        assert formal["generation"] == 2  # 不抬升
        assert raw.execute(
            "SELECT COUNT(*) c FROM entity_generations WHERE entity_key='NEW-100'").fetchone()["c"] == 1
        ev = [r["event"] for r in raw.execute(
            "SELECT event FROM replay_audit WHERE conflict_id=? ORDER BY id", (cid2,))]
        assert ev == ["replay_skip"]
        cf = raw.execute("SELECT * FROM conflicts WHERE id=?", (cid2,)).fetchone()
        assert cf["status"] == "已重放" and cf["replay_id"] is not None


# 乙路径（新插入分支）：正式区无此键 -> generation 从 1 起，无被盖快照
def test_replay_insert_path_starts_generation_one_without_snapshot(_db):
    c = _client(); h = _auth(c)
    _add_mapping(_db, "OLD-C9", "NEW-999")
    cid = _conflict_id(c, h)
    body = c.post(f"/api/conflicts/{cid}/replay", headers=h).json()
    assert body["path"] == "insert" and body["generation"] == 1 and body["snapshot_id"] is None
    with _raw(_db) as raw:
        assert raw.execute(
            "SELECT COUNT(*) c FROM entity_generations WHERE entity_key='NEW-999'").fetchone()["c"] == 0
        audit = raw.execute(
            "SELECT * FROM replay_audit WHERE conflict_id=?", (cid,)).fetchone()
        assert audit["event"] == "replay_apply"
        assert audit["generation_before"] == 0 and audit["generation_after"] == 1


# 乙路径事务性：快照写入被注入失败时，正式表与审计必须一起回滚，不留半条
def test_replay_overwrite_rolls_back_formal_and_snapshot_together(_db):
    c = _client(); h = _auth(c)
    _add_mapping(_db, "OLD-C9", "NEW-100")  # 与正式区 NEW-100/OLD-A1 不同 -> 覆盖
    cid = _conflict_id(c, h)

    with _raw(_db) as raw:
        raw.execute(
            "CREATE TRIGGER fail_snapshot BEFORE INSERT ON entity_generations "
            "BEGIN SELECT RAISE(ABORT,'injected snapshot failure'); END")

    r = c.post(f"/api/conflicts/{cid}/replay", headers=h)
    assert r.status_code == 500

    with _raw(_db) as raw:
        formal = raw.execute("SELECT * FROM formal_entities WHERE entity_key='NEW-100'").fetchone()
        assert formal["mapped_from"] == "OLD-A1"  # 正式内容未被改
        assert formal["generation"] == 1
        assert raw.execute("SELECT COUNT(*) c FROM replay_audit").fetchone()["c"] == 0
        assert raw.execute("SELECT COUNT(*) c FROM entity_generations").fetchone()["c"] == 0
        assert raw.execute("SELECT COUNT(*) c FROM replay_locks").fetchone()["c"] == 0
        cf = raw.execute("SELECT status FROM conflicts WHERE id=?", (cid,)).fetchone()
        assert cf["status"] == "挂起"  # 冲突仍可重新重放


# 偏离路径：重放时仍无映射 -> 拒写并记 replay_deviation，正式零改
def test_replay_without_mapping_records_deviation_and_refuses_write(_db):
    c = _client(); h = _auth(c)
    cid = _conflict_id(c, h)  # OLD-C9 全程无映射
    body = c.post(f"/api/conflicts/{cid}/replay", headers=h).json()
    assert body["path"] == "deviation"
    with _raw(_db) as raw:
        ev = [r["event"] for r in raw.execute(
            "SELECT event FROM replay_audit WHERE conflict_id=?", (cid,))]
        assert ev == ["replay_deviation"]
        assert raw.execute("SELECT COUNT(*) c FROM formal_entities").fetchone()["c"] == 1
        cf = raw.execute("SELECT * FROM conflicts WHERE id=?", (cid,)).fetchone()
        assert cf["status"] == "挂起"  # 未冒充成功


# 五处同态：互证页把正式世代、世代链、审计、冲突对齐，无 mismatch / 孤儿冲突
def test_crosscheck_is_consistent_across_five_places(_db):
    c = _client(); h = _auth(c)
    _add_mapping(_db, "OLD-C9", "NEW-100")
    cid = _conflict_id(c, h)
    c.post(f"/api/conflicts/{cid}/replay", headers=h)

    r = c.get("/api/crosscheck", headers=h)
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["orphan_conflict_ids"] == []
    by_key = {x["entity_key"]: x for x in payload["items"]}
    assert by_key["NEW-100"]["formal_generation"] == 2
    assert by_key["NEW-100"]["chain_max_generation"] == 1
    assert by_key["NEW-100"]["snapshot_count"] == 1
    assert by_key["NEW-100"]["apply_audit_count"] == 1
    assert cid in by_key["NEW-100"]["linked_conflict_ids"]

    # 世代列表与审计接口均可查
    gens = c.get("/api/generations", headers=h).json()["items"]
    assert len(gens) == 1 and gens[0]["mapped_from"] == "OLD-A1" and gens[0]["generation"] == 1
    audits = c.get("/api/replay-audits", headers=h).json()["items"]
    assert any(a["event"] == "replay_apply" and a["snapshot_id"] == gens[0]["id"] for a in audits)

    # 断链注入：手改正式世代后互证必须报 mismatch
    with _raw(_db) as raw:
        raw.execute("UPDATE formal_entities SET generation=9 WHERE entity_key='NEW-100'")
    payload2 = c.get("/api/crosscheck", headers=h).json()
    assert payload2["ok"] is False
    assert any(x["status"] == "mismatch" for x in payload2["items"])


# 登录沿用：未带令牌一律 401，重放不另开认证
def test_replay_requires_existing_login(_db):
    c = _client()
    r = c.post("/api/conflicts/1/replay")
    assert r.status_code == 401
    assert c.get("/api/replay-audits").status_code == 401
    assert c.get("/api/crosscheck").status_code == 401
