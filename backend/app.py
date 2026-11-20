
import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 争用快速失败：后到者只记争用，不排队改写
LOCK_BUSY_TIMEOUT_SECONDS = 0.2


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=LOCK_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # WAL：读写并发下写锁即时可见，占锁不会退化为排队等待
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=%d" % int(LOCK_BUSY_TIMEOUT_SECONDS * 1000))
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn):
    """老库平滑加列；新库由 init_db 直接建全量结构。"""
    cols = _columns(conn, "formal_entities")
    if "generation" not in cols:
        conn.execute("ALTER TABLE formal_entities ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
    ccols = _columns(conn, "conflicts")
    if "replay_id" not in ccols:
        conn.execute("ALTER TABLE conflicts ADD COLUMN replay_id INTEGER")


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT UNIQUE NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          generation INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          replay_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS replay_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          conflict_id INTEGER NOT NULL,
          event TEXT NOT NULL,
          entity_key TEXT,
          generation_before INTEGER,
          generation_after INTEGER,
          snapshot_id INTEGER,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entity_generations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          generation INTEGER NOT NULL,
          superseded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS replay_locks(
          conflict_id INTEGER PRIMARY KEY,
          holder TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        """)
        _migrate(conn)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at,generation) VALUES (?,?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00",1))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))


@app.on_event("startup")
def _s(): init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}


@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}


def _upsert_formal(conn, entity_key: str, mapped_from: str, version: str, now: str) -> str:
    """正式区唯一写入口：同键只留当前世代。显式 SELECT 判定，不依赖库表是否有 UNIQUE 约束
    （老库 formal_entities 无唯一索引，靠应用层保证单键单行）。"""
    row = conn.execute("SELECT id, generation FROM formal_entities WHERE entity_key=?", (entity_key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO formal_entities(entity_key,mapped_from,version,created_at,generation) VALUES (?,?,?,?,1)",
            (entity_key, mapped_from, version, now))
        return "insert"
    conn.execute(
        "UPDATE formal_entities SET mapped_from=?, version=?, created_at=? WHERE id=?",
        (mapped_from, version, now, row["id"]))
    return "update"


class RemapIn(BaseModel):
    old_key: str


@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m:
            c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")
        _upsert_formal(c, m["new_key"], body.old_key, ver["version"], now)
        c.commit()
        return {"entity_key": m["new_key"], "version": ver["version"]}


@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}


@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}


def _write_audit(conn, conflict_id, event, entity_key, before, after, snapshot_id, detail):
    cur = conn.execute(
        """INSERT INTO replay_audit(conflict_id,event,entity_key,generation_before,generation_after,
           snapshot_id,detail,created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (conflict_id, event, entity_key, before, after, snapshot_id, detail,
         datetime.now().isoformat(timespec="seconds")))
    return cur.lastrowid


def _release_lock(conn, conflict_id):
    """占锁是互斥原语而非业务数据：成功提交前主动删除，与业务改动同事务进出。"""
    conn.execute("DELETE FROM replay_locks WHERE conflict_id=?", (conflict_id,))


def _record_contention(conflict_id, detail):
    """争用审计走独立短连接并等写锁窗口：争用者自己的写事务未开成，
    不能在被锁连接上写；审计可等待，业务重放本身不排队。"""
    ac = connect()
    ac.execute("PRAGMA busy_timeout=5000")  # 审计最多等赢家提交 5s
    try:
        _write_audit(ac, conflict_id, "replay_contention", None, None, None, None, detail)
        ac.commit()
    finally:
        ac.close()


@app.post("/api/conflicts/{conflict_id}/replay")
def replay_conflict(conflict_id: int, authorization: str | None = Header(default=None)):
    """冲突重放唯一入口（沿用冲突区，不另起重放台）。甲/乙互斥，同事务，占锁。"""
    user = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        # 显式开启事务：占锁到审计/正式写全程同事务，提交/回滚统一收口
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # 库级写锁忙等待超时（赢家正持写事务）：本连接无事务、零写入，判争用
            _record_contention(conflict_id, "重放事务占库写锁超时，判争用，正式零改")
            raise HTTPException(409, "replay_contention")
        # 占锁：同 conflict_id 后到者只记争用，不排队。
        # 两类失败都算争用：唯一键冲突（锁行已在）；库级写锁忙超时（赢家正持 BEGIN IMMEDIATE）
        try:
            conn.execute(
                "INSERT INTO replay_locks(conflict_id,holder,acquired_at) VALUES (?,?,?)",
                (conflict_id, user, now))
        except sqlite3.IntegrityError:
            _write_audit(conn, conflict_id, "replay_contention", None, None, None, None,
                         "同 conflict_id 重放争用，后到者未取得占锁，正式零改")
            conn.commit()
            raise HTTPException(409, "replay_contention")
        except sqlite3.OperationalError:
            # WAL 下赢家持写锁：本事务未写入任何内容，回滚后经独立连接只记争用
            conn.rollback()
            _record_contention(conflict_id, "同 conflict_id 重放争用（写锁忙），后到者未取得占锁，正式零改")
            raise HTTPException(409, "replay_contention")

        conflict = conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if conflict is None:
            conn.rollback()  # 连同占锁一起释放
            raise HTTPException(404, "冲突不存在")
        # 占锁判定前置于状态检查：后到者拿不到锁只记争用，不依据状态走别的分支
        if conflict["status"] == "已重放":
            conn.rollback()
            raise HTTPException(409, "冲突已重放，禁止重复重放")

        ver = conn.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver:
            conn.rollback()
            raise HTTPException(404, "无生效映射版本")
        m = conn.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                         (ver["version"], conflict["old_key"])).fetchone()
        if not m:
            # 仍无映射：留在冲突区，记偏离审计，不触发任何正式写
            audit_id = _write_audit(conn, conflict_id, "replay_deviation", None, None, None, None,
                                    "重放时仍无生效映射，拒写正式区")
            conn.execute("UPDATE conflicts SET reason=? WHERE id=?", ("重放拒写：无映射", conflict_id))
            _release_lock(conn, conflict_id)
            conn.commit()
            return {"path": "deviation", "audit_id": audit_id, "detail": "无映射，拒写"}

        entity_key, mapped_from, version = m["new_key"], conflict["old_key"], ver["version"]
        current = conn.execute("SELECT * FROM formal_entities WHERE entity_key=?", (entity_key,)).fetchone()

        # 甲路径：目标已在正式且内容一致 -> 幂等跳过，正式零改
        if current is not None and current["mapped_from"] == mapped_from and current["version"] == version:
            audit_id = _write_audit(
                conn, conflict_id, "replay_skip", entity_key,
                current["generation"], current["generation"], None,
                "目标已在正式区且内容一致，幂等跳过，正式零改，generation 不抬升")
            conn.execute("UPDATE conflicts SET status=?, replay_id=? WHERE id=?",
                         ("已重放", audit_id, conflict_id))
            _release_lock(conn, conflict_id)
            conn.commit()
            return {"path": "skip", "entity_key": entity_key,
                    "generation": current["generation"], "audit_id": audit_id}

        # 乙路径：覆盖/新插入，快照、抬 generation、审计、冲突回写必须同事务
        before_generation = current["generation"] if current is not None else 0
        snapshot_id = None
        if current is not None:
            snap = conn.execute(
                """INSERT INTO entity_generations(entity_key,mapped_from,version,created_at,generation,superseded_at)
                   VALUES (?,?,?,?,?,?)""",
                (current["entity_key"], current["mapped_from"], current["version"],
                 current["created_at"], current["generation"], now))
            snapshot_id = snap.lastrowid

        _upsert_formal(conn, entity_key, mapped_from, version, now)
        after_generation = before_generation + 1
        conn.execute("UPDATE formal_entities SET generation=? WHERE entity_key=?",
                     (after_generation, entity_key))

        path = "overwrite" if current is not None else "insert"
        audit_id = _write_audit(
            conn, conflict_id, "replay_apply", entity_key,
            before_generation, after_generation, snapshot_id,
            ("覆盖正式实体并抬升 generation，被盖版本已入世代快照"
             if current is not None else "新实体进入正式区，generation 从 1 起"))
        conn.execute("UPDATE conflicts SET status=?, replay_id=? WHERE id=?",
                     ("已重放", audit_id, conflict_id))
        _release_lock(conn, conflict_id)
        conn.commit()  # 任一步抛错都会走到 rollback：正式与快照同回滚，审计不留半条
        return {"path": path, "entity_key": entity_key,
                "generation": after_generation, "audit_id": audit_id,
                "snapshot_id": snapshot_id}
    except HTTPException:
        raise
    except Exception as exc:
        # 任一步失败：正式与快照同事务回滚，审计不留半条，锁随回滚释放
        conn.rollback()
        raise HTTPException(500, "replay_failed_rolled_back") from exc
    finally:
        conn.close()


@app.get("/api/replay-audits")
def replay_audits(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM replay_audit ORDER BY id DESC").fetchall()]}


@app.get("/api/generations")
def generations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM entity_generations ORDER BY id DESC").fetchall()]}


@app.get("/api/crosscheck")
def crosscheck(authorization: str | None = Header(default=None)):
    """互证页：正式现态 × 世代链 × 重放审计 × 冲突区，按 entity_key+generation 同态对账。"""
    require_user(authorization)
    with connect() as c:
        rows = []
        for f in c.execute("SELECT * FROM formal_entities ORDER BY entity_key").fetchall():
            chain = c.execute(
                "SELECT COALESCE(MAX(generation),0) mg, COUNT(*) n FROM entity_generations WHERE entity_key=?",
                (f["entity_key"],)).fetchone()
            applies = c.execute(
                "SELECT COUNT(*) n FROM replay_audit WHERE entity_key=? AND event='replay_apply'",
                (f["entity_key"],)).fetchone()["n"]
            expected_generation = chain["mg"] + 1
            mismatches = []
            if f["generation"] != expected_generation:
                mismatches.append("正式世代与世代链断链")
            # 每次覆盖型 apply 都必须能指到一条被盖快照
            overwrites = c.execute(
                "SELECT COUNT(*) n FROM replay_audit WHERE entity_key=? AND event='replay_apply' AND snapshot_id IS NOT NULL",
                (f["entity_key"],)).fetchone()["n"]
            if overwrites != chain["n"]:
                mismatches.append("覆盖审计与被盖快照数量不符")
            linked_conflicts = c.execute(
                """SELECT cf.id FROM conflicts cf JOIN replay_audit ra ON cf.replay_id=ra.id
                   WHERE ra.entity_key=?""", (f["entity_key"],)).fetchall()
            rows.append({
                "entity_key": f["entity_key"],
                "formal_generation": f["generation"],
                "formal_mapped_from": f["mapped_from"],
                "formal_version": f["version"],
                "chain_max_generation": chain["mg"],
                "snapshot_count": chain["n"],
                "apply_audit_count": applies,
                "linked_conflict_ids": [r["id"] for r in linked_conflicts],
                "status": "mismatch" if mismatches else "ok",
                "mismatches": mismatches,
            })
        # 已重放冲突必须有 apply 或 skip 审计
        orphan_conflicts = c.execute("""
            SELECT cf.id FROM conflicts cf
            LEFT JOIN replay_audit ra ON ra.conflict_id=cf.id
            WHERE cf.status='已重放' AND ra.id IS NULL""").fetchall()
        return {"items": rows,
                "orphan_conflict_ids": [r["id"] for r in orphan_conflicts],
                "ok": not orphan_conflicts and all(r["status"] == "ok" for r in rows)}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
