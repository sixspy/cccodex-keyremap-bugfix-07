
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import DB_PATH, app, init_db


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


def _seed_conflict(c, h, old_key="OLD-C9"):
    rows = c.get("/api/conflicts", headers=h).json()["items"]
    return next(x["id"] for x in rows if x["old_key"] == old_key)


# 红测 1：先复现「覆盖无痕」——异内容重放覆盖正式实体时，
# 必须同事务落 apply 审计、抬 generation、留被盖快照，缺一即失败。
def test_replay_overwrite_without_audit_is_traced(_db):
    c = _client()
    h = _auth(c)
    # 正式区先有一条 NEW-200（来自 OLD-B1），世代为 1
    assert c.post("/api/remap", headers=h, json={"old_key": "OLD-B1"}).status_code == 200
    conflict_id = _seed_conflict(c, h)

    # 给冲突旧键补一条指向同一正式键的映射，使重放结果与现态不同（OLD-C9 -> NEW-200）
    with _raw(_db) as raw:
        ver = raw.execute("SELECT version FROM map_versions WHERE status='生效'").fetchone()["version"]
        raw.execute("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
                    (ver, "OLD-C9", "NEW-200", "一对一"))

    r = c.post(f"/api/conflicts/{conflict_id}/replay", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "overwrite"
    assert r.json()["generation"] == 2

    with _raw(_db) as raw:
        formal = raw.execute(
            "SELECT * FROM formal_entities WHERE entity_key='NEW-200'").fetchone()
        assert formal is not None
        assert formal["mapped_from"] == "OLD-C9"
        assert formal["generation"] == 2  # generation 必须抬升

        audit = raw.execute(
            "SELECT * FROM replay_audit WHERE conflict_id=? ORDER BY id", (conflict_id,)).fetchall()
        assert len(audit) == 1
        assert audit[0]["event"] == "replay_apply"  # 覆盖必须有审计，无痕即红
        assert audit[0]["generation_after"] == 2
        assert audit[0]["generation_before"] == 1

        snap = raw.execute(
            "SELECT * FROM entity_generations WHERE entity_key='NEW-200' ORDER BY id").fetchall()
        assert len(snap) == 1  # 被盖版本必须可追溯
        assert snap[0]["mapped_from"] == "OLD-B1"
        assert snap[0]["generation"] == 1

        conflict = raw.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        assert conflict["status"] == "已重放"
        assert conflict["replay_id"] == audit[0]["id"]


# 红测 2：同 conflict_id 两路重放抢写必须占锁，
# 后到者判争用（409），只落 contention 审计，正式零改。
def test_concurrent_replay_same_conflict_lock_contention(_db, monkeypatch):
    import app as appmod

    c = _client()
    h = _auth(c)
    assert c.post("/api/remap", headers=h, json={"old_key": "OLD-B1"}).status_code == 200
    conflict_id = _seed_conflict(c, h)
    with _raw(_db) as raw:
        ver = raw.execute("SELECT version FROM map_versions WHERE status='生效'").fetchone()["version"]
        raw.execute("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
                    (ver, "OLD-C9", "NEW-200", "一对一"))

    # 撑开赢家持锁窗口：赢家进入正式写后停留，保证输家撞上的是占锁而非已完成状态。
    # 不用屏障——输家在等锁，永远到不了钩子，屏障会自死锁。
    import time
    real_upsert = appmod._upsert_formal

    def slow_upsert(conn, *a, **kw):
        time.sleep(0.8)
        return real_upsert(conn, *a, **kw)

    monkeypatch.setattr(appmod, "_upsert_formal", slow_upsert)
    results = []

    def winner():
        cc = _client()
        hh = _auth(cc)
        rr = cc.post(f"/api/conflicts/{conflict_id}/replay", headers=hh)
        results.append((rr.status_code, rr.json()))

    def loser():
        time.sleep(0.02)  # 赢家持锁窗口有 0.8s，此刻必在其事务内
        cc = _client()
        hh = _auth(cc)
        rr = cc.post(f"/api/conflicts/{conflict_id}/replay", headers=hh)
        results.append((rr.status_code, rr.json()))

    t1 = threading.Thread(target=winner)
    t2 = threading.Thread(target=loser)
    t1.start(); t2.start()
    t1.join(15); t2.join(15)

    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 409], results
    loser_body = next(j for s, j in results if s == 409)
    assert loser_body["detail"] == "replay_contention"

    with _raw(_db) as raw:
        formal = raw.execute(
            "SELECT * FROM formal_entities WHERE entity_key='NEW-200'").fetchone()
        assert formal["generation"] == 2  # 只允许一路生效，正式未被二次改写
        events = [r["event"] for r in raw.execute(
            "SELECT event FROM replay_audit WHERE conflict_id=? ORDER BY id", (conflict_id,))]
        assert events == ["replay_apply", "replay_contention"]
        assert raw.execute(
            "SELECT COUNT(*) c FROM replay_locks").fetchone()["c"] == 0  # 锁随事务释放
        snaps = raw.execute(
            "SELECT COUNT(*) c FROM entity_generations WHERE entity_key='NEW-200'").fetchone()["c"]
        assert snaps == 1
