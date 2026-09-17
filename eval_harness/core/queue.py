"""eval_harness · 任务队列（Phase 3 · 跨进程 Worker 水平扩容）

设计目标：
- 抽象 JobQueue：enqueue / claim / complete / fail / 状态查询。
- 本地零依赖实现 SqliteJobQueue：单文件 SQLite，原子 claim 模拟
  PG 的 `FOR UPDATE SKIP LOCKED`（多 Worker 进程安全抢任务，不重不漏）。
- 生产替换点 RedisJobQueue（可选，需 redis）：同一抽象，换后端即可接入
  真实分布式 / 跨机集群，引擎与 Worker 代码不变。

解决常见开源框架痛点：单进程串行、无法水平扩容到多机多卡。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional


class JobQueue:
    """任务队列抽象。"""

    def enqueue(self, items: list[dict]):
        raise NotImplementedError

    def claim(self, timeout: float = 1.0) -> Optional[dict]:
        """原子认领一个 queued 任务，返回 {id, payload} 或 None。"""
        raise NotImplementedError

    def complete(self, job_id, result: str):
        raise NotImplementedError

    def fail(self, job_id, error: str):
        raise NotImplementedError

    def pending(self) -> int:
        raise NotImplementedError

    def running(self) -> int:
        raise NotImplementedError

    def remaining(self) -> int:
        """仍未结束（queued + running）的任务数，Worker 据此判断是否退出。"""
        return self.pending() + self.running()

    def done_count(self) -> int:
        raise NotImplementedError

    def iter_results(self) -> list[dict]:
        """返回已完成任务的 result 行（JSON 字符串）。"""
        raise NotImplementedError

    def reset_running(self):
        """将残留 running 任务回退为 queued（Worker 异常退出后的孤儿恢复）。"""
        raise NotImplementedError


class SqliteJobQueue(JobQueue):
    """基于单文件 SQLite 的本地队列（零依赖，跨进程安全）。

    claim 用「SELECT 取一个 queued id → UPDATE 置 running 且校验 affected 行数」
    的乐观锁模式，等价于 SKIP LOCKED 的语义：多个进程并发 claim 不会拿到同一任务。
    """

    def __init__(self, path: str = ":memory:"):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                result TEXT,
                error TEXT,
                owner TEXT,
                claimed_at REAL
            )"""
        )
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass

    def enqueue(self, items: list[dict]):
        with self._lock:
            self._conn.executemany(
                "INSERT INTO jobs (payload) VALUES (?)",
                [(json.dumps(it, ensure_ascii=False),) for it in items],
            )
            self._conn.commit()

    def claim(self, timeout: float = 1.0) -> Optional[dict]:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                jid = row[0]
                cur = self._conn.execute(
                    "UPDATE jobs SET status='running', claimed_at=? WHERE id=? AND status='queued'",
                    (time.time(), jid),
                )
                if cur.rowcount == 1:
                    payload = self._conn.execute(
                        "SELECT payload FROM jobs WHERE id=?", (jid,)
                    ).fetchone()[0]
                    self._conn.commit()
                    return {"id": jid, "payload": payload}
                # 被别的进程抢走，立即重试
        return None

    def complete(self, job_id, result: str):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='done', result=? WHERE id=?",
                (result, job_id),
            )
            self._conn.commit()

    def fail(self, job_id, error: str):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='failed', error=? WHERE id=?",
                (error, job_id),
            )
            self._conn.commit()

    def reset_running(self):
        with self._lock:
            self._conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            self._conn.commit()

    def pending(self) -> int:
        return self._count("queued")

    def running(self) -> int:
        return self._count("running")

    def done_count(self) -> int:
        return self._count("done") + self._count("failed")

    def _count(self, status: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status=?", (status,)
            ).fetchone()[0]

    def iter_results(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result FROM jobs WHERE status='done' AND result IS NOT NULL"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]


class RedisJobQueue(JobQueue):
    """生产替换点：Redis 列表/BRPOP 实现（需 pip install redis）。

    接口与 SqliteJobQueue 完全一致；接入后引擎与 Worker 代码无需改动即可跨机扩容。
    此处仅占位，避免硬依赖。
    """

    def __init__(self, url: str = "redis://localhost:6379/0", key: str = "eval_harness:jobs"):
        import redis  # 延迟导入，未安装时不影响本地模式

        self._r = redis.from_url(url)
        self._key = key

    def enqueue(self, items: list[dict]):
        self._r.rpush(self._key, *[json.dumps(it, ensure_ascii=False) for it in items])

    def claim(self, timeout: float = 1.0) -> Optional[dict]:
        res = self._r.blpop(self._key, timeout=int(timeout))
        if res is None:
            return None
        return {"id": "redis", "payload": res[1]}

    def complete(self, job_id, result: str):  # pragma: no cover
        self._r.rpush(self._key + ":done", result)

    def fail(self, job_id, error: str):  # pragma: no cover
        self._r.rpush(self._key + ":failed", error)

    def pending(self) -> int:  # pragma: no cover
        return self._r.llen(self._key)

    def running(self) -> int:  # pragma: no cover
        return 0

    def done_count(self) -> int:  # pragma: no cover
        return self._r.llen(self._key + ":done")

    def iter_results(self) -> list[dict]:  # pragma: no cover
        out = []
        while True:
            v = self._r.lpop(self._key + ":done")
            if v is None:
                break
            out.append(json.loads(v))
        return out

    def reset_running(self):  # pragma: no cover
        return None
