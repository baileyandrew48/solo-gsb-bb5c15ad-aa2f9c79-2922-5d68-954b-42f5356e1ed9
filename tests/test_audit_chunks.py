"""POST /sessions/{id}/chunks/audit：上传中会话的分片完整性诊断。"""
from __future__ import annotations

import hashlib
import threading

from fastapi.testclient import TestClient

from app import storage


def _create(client: TestClient, data: bytes, name: str = "data.bin", chunk_size: int = 0):
    resp = client.post(
        "/api/v1/sessions",
        json={
            "filename": name,
            "total_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "chunk_size": chunk_size,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _put(client: TestClient, sid: str, start: int, end: int, content: bytes, **kw):
    return client.put(
        f"/api/v1/sessions/{sid}/chunks?start={start}&end={end}",
        content=content,
        **kw,
    )


def _audit(client: TestClient, sid: str, items: list[dict], **kw):
    return client.post(
        f"/api/v1/sessions/{sid}/chunks/audit",
        json={"chunks": items},
        **kw,
    )


def _manifest(piece: bytes, start: int, end: int) -> dict:
    return {"start": start, "end": end, "sha256": hashlib.sha256(piece).hexdigest()}


def _upload_pieces(client: TestClient, sid: str, bounds: list[int], data: bytes) -> list[bytes]:
    pieces = [data[s:e] for s, e in zip(bounds, bounds[1:])]
    for (s, e), piece in zip(zip(bounds, bounds[1:]), pieces):
        _put(client, sid, s, e, piece).raise_for_status()
    return pieces


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_audit_all_matched_sorted_by_start(client: TestClient) -> None:
    data = bytes((i * 7 + 3) % 256 for i in range(6000))
    sid = _create(client, data)
    bounds = [0, 100, 2500, 6000]
    pieces = _upload_pieces(client, sid, bounds, data)

    # 清单故意乱序提交，响应仍按 start 升序。
    items = [
        _manifest(pieces[2], 2500, 6000),
        _manifest(pieces[0], 0, 100),
        _manifest(pieces[1], 100, 2500),
    ]
    r = _audit(client, sid, items)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == sid
    assert [row["start"] for row in body["results"]] == [0, 100, 2500]
    for row, piece, (s, e) in zip(body["results"], pieces, zip(bounds, bounds[1:])):
        assert row == {
            "start": s,
            "end": e,
            "expected_sha256": hashlib.sha256(piece).hexdigest(),
            "status": "ok",
            "actual_size": e - s,
            "actual_sha256": hashlib.sha256(piece).hexdigest(),
        }


def test_audit_subset_of_registered_chunks(client: TestClient) -> None:
    data = b"".join(bytes([i % 256]) * 500 for i in range(6))  # 3000, 6 片
    sid = _create(client, data)
    bounds = list(range(0, 3001, 500))
    pieces = _upload_pieces(client, sid, bounds, data)

    # 只核对其中两片（允许清单是已登记分片的子集）。
    r = _audit(client, sid, [
        _manifest(pieces[0], 0, 500),
        _manifest(pieces[4], 2000, 2500),
    ])
    assert r.status_code == 200, r.text
    assert [row["status"] for row in r.json()["results"]] == ["ok", "ok"]


def test_audit_reports_digest_mismatch_with_actual_digest(client: TestClient) -> None:
    data = bytes((i * 13 + 1) % 256 for i in range(3000))
    sid = _create(client, data)
    bounds = [0, 1000, 3000]
    pieces = _upload_pieces(client, sid, bounds, data)

    # 等长损坏磁盘上的第二片。
    corrupted = b"!" + pieces[1][1:]
    storage.part_path(sid, 1000, 3000).write_bytes(corrupted)

    r = _audit(client, sid, [_manifest(p, s, e) for p, (s, e) in zip(pieces, zip(bounds, bounds[1:]))])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["status"] == "ok"
    bad = results[1]
    assert bad["status"] == "digest_mismatch"
    assert bad["actual_size"] == 2000
    assert bad["actual_sha256"] == hashlib.sha256(corrupted).hexdigest()
    assert bad["expected_sha256"] == hashlib.sha256(pieces[1]).hexdigest()
    # 客户端据诊断结果做 repair 后重新诊断即全部 ok。
    _put_repair = client.put(
        f"/api/v1/sessions/{sid}/chunks/repair?start=1000&end=3000",
        content=pieces[1],
    )
    assert _put_repair.status_code == 200, _put_repair.text
    r2 = _audit(client, sid, [_manifest(p, s, e) for p, (s, e) in zip(pieces, zip(bounds, bounds[1:]))])
    assert {row["status"] for row in r2.json()["results"]} == {"ok"}


def test_audit_reports_missing_file(client: TestClient) -> None:
    data = b"m" * 2000
    sid = _create(client, data)
    _put(client, sid, 0, 1000, data[:1000]).raise_for_status()
    _put(client, sid, 1000, 2000, data[1000:]).raise_for_status()

    storage.part_path(sid, 1000, 2000).unlink()

    r = _audit(client, sid, [
        _manifest(data[:1000], 0, 1000),
        _manifest(data[1000:], 1000, 2000),
    ])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["status"] == "ok"
    assert results[1] == {
        "start": 1000,
        "end": 2000,
        "expected_sha256": hashlib.sha256(data[1000:]).hexdigest(),
        "status": "missing",
        "actual_size": None,
        "actual_sha256": None,
    }
    # 登记仍在：repair 可直接补齐缺失文件。
    rr = client.put(
        f"/api/v1/sessions/{sid}/chunks/repair?start=1000&end=2000",
        content=data[1000:],
    )
    assert rr.status_code == 200, rr.text


def test_audit_reports_length_mismatch_with_actual_size_and_digest(
    client: TestClient,
) -> None:
    data = b"l" * 1500
    sid = _create(client, data)
    _put(client, sid, 0, 1500, data).raise_for_status()

    # 外部破坏：磁盘文件被截短（登记长度仍是 1500）。
    short = b"s" * 800
    storage.part_path(sid, 0, 1500).write_bytes(short)

    r = _audit(client, sid, [_manifest(data, 0, 1500)])
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert row["status"] == "length_mismatch"
    assert row["actual_size"] == 800
    assert row["actual_sha256"] == hashlib.sha256(short).hexdigest()


def test_audit_accepts_sha_prefix_and_uppercase(client: TestClient) -> None:
    data = b"x" * 100
    sid = _create(client, data)
    _put(client, sid, 0, 100, data).raise_for_status()
    digest = hashlib.sha256(data).hexdigest()
    r = _audit(client, sid, [{"start": 0, "end": 100, "sha256": "SHA256:" + digest.upper()}])
    assert r.status_code == 200, r.text
    # 响应中的期望摘要已规整为小写裸 hex。
    assert r.json()["results"][0]["expected_sha256"] == digest


# ---------------------------------------------------------------------------
# 非法清单：整批拒绝（400/422），不返回逐条结果
# ---------------------------------------------------------------------------


def test_audit_unregistered_range_whole_batch_rejected_400(client: TestClient) -> None:
    data = b"u" * 3000
    sid = _create(client, data)
    _put(client, sid, 0, 1000, data[:1000]).raise_for_status()

    before = client.get(f"/api/v1/sessions/{sid}").json()

    # 完全未登记区间。
    r = _audit(client, sid, [
        _manifest(data[:1000], 0, 1000),
        _manifest(data[1000:2000], 1000, 2000),
    ])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "audit manifest ranges must exactly match registered chunks"
    assert detail["unregistered"] == [{"start": 1000, "end": 2000}]

    # 部分重叠 / 跨分片边界 / 仅相邻，都不算精确对应。
    r = _audit(client, sid, [{"start": 0, "end": 2000, "sha256": "a" * 64}])
    assert r.status_code == 400
    r = _audit(client, sid, [{"start": 500, "end": 1000, "sha256": "a" * 64}])
    assert r.status_code == 400
    r = _audit(client, sid, [{"start": 1000, "end": 2000, "sha256": "a" * 64}])
    assert r.status_code == 400

    # 整批拒绝：登记、字节数、缺失区间与磁盘均不变。
    after = client.get(f"/api/v1/sessions/{sid}").json()
    assert after["bytes_received"] == before["bytes_received"] == 1000
    assert after["missing_ranges"] == before["missing_ranges"]


def test_audit_duplicate_range_whole_batch_rejected_400(client: TestClient) -> None:
    data = b"d" * 1000
    sid = _create(client, data)
    _put(client, sid, 0, 1000, data).raise_for_status()

    item = _manifest(data, 0, 1000)
    r = _audit(client, sid, [item, dict(item)])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "audit manifest contains duplicate ranges"
    assert detail["duplicates"] == [{"start": 0, "end": 1000}]


def test_audit_empty_manifest_is_422(client: TestClient) -> None:
    data = b"e" * 100
    sid = _create(client, data)
    _put(client, sid, 0, 100, data).raise_for_status()
    r = _audit(client, sid, [])
    assert r.status_code == 422


def test_audit_more_than_32_items_is_422(client: TestClient) -> None:
    # 无需登记任何分片：请求体形态在进入业务逻辑前即被拒绝。
    sid = _create(client, b"z")
    items = [{"start": i, "end": i + 1, "sha256": "a" * 64} for i in range(33)]
    r = _audit(client, sid, items)
    assert r.status_code == 422
    # 32 条恰好通过形态校验（随后因未登记得到 400，而非 422）。
    r32 = _audit(client, sid, items[:32])
    assert r32.status_code == 400


def test_audit_invalid_item_shapes_are_422(client: TestClient) -> None:
    sid = _create(client, b"q")
    bad_bodies = [
        {"chunks": [{"start": -1, "end": 10, "sha256": "a" * 64}]},
        {"chunks": [{"start": 10, "end": 10, "sha256": "a" * 64}]},
        {"chunks": [{"start": 20, "end": 10, "sha256": "a" * 64}]},
        {"chunks": [{"start": 0, "end": 10, "sha256": "nope"}]},
        {"chunks": [{"start": 0, "end": 10, "sha256": "g" * 64}]},
        {},
    ]
    for body in bad_bodies:
        r = client.post(f"/api/v1/sessions/{sid}/chunks/audit", json=body)
        assert r.status_code == 422, (body, r.text)


# ---------------------------------------------------------------------------
# 只读语义：不滑动 TTL、不改登记与已收字节
# ---------------------------------------------------------------------------


def test_audit_does_not_slide_ttl_or_change_state(client: TestClient) -> None:
    data = b"t" * 2000
    sid = _create(client, data)
    _put(client, sid, 0, 1000, data[:1000]).raise_for_status()
    _put(client, sid, 1000, 2000, data[1000:]).raise_for_status()

    # 制造一片损坏后多次诊断（含 ok / 摘要不符 / 文件缺失）。
    storage.part_path(sid, 1000, 2000).write_bytes(b"!" * 1000)
    before = client.get(f"/api/v1/sessions/{sid}").json()

    for _ in range(3):
        r = _audit(client, sid, [
            _manifest(data[:1000], 0, 1000),
            _manifest(data[1000:], 1000, 2000),
        ])
        assert r.status_code == 200, r.text

    after = client.get(f"/api/v1/sessions/{sid}").json()
    assert after["expires_at"] == before["expires_at"]
    assert after["last_access_at"] == before["last_access_at"]
    assert after["bytes_received"] == before["bytes_received"] == 2000
    assert after["missing_ranges"] == before["missing_ranges"] == []
    # 诊断不产生任何临时文件。
    assert not list(storage.tmp_dir().glob("*.part"))


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


def test_audit_unknown_session_is_404(client: TestClient) -> None:
    r = _audit(client, "deadbeef" * 4, [{"start": 0, "end": 1, "sha256": "a" * 64}])
    assert r.status_code == 404


def test_audit_completed_session_rejected_409(client: TestClient) -> None:
    data = b"done" * 100
    sid = _create(client, data)
    _put(client, sid, 0, len(data), data).raise_for_status()
    r = client.post(f"/api/v1/sessions/{sid}/complete")
    assert r.status_code == 200, r.text
    r = _audit(client, sid, [_manifest(data, 0, len(data))])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "session already completed; nothing to audit"
    # 成品不受影响。
    assert client.get(f"/api/v1/sessions/{sid}/download").content == data


def test_audit_expired_session_is_410(client: TestClient, monkeypatch) -> None:
    from app import settings
    import time

    data = b"e" * 300
    monkeypatch.setattr(settings, "ttl_seconds", 1)
    sid = _create(client, data)
    _put(client, sid, 0, 300, data).raise_for_status()
    time.sleep(1.1)
    r = _audit(client, sid, [_manifest(data, 0, 300)])
    assert r.status_code == 410
    assert not storage.parts_dir(sid).exists()


def test_audit_aborted_session_is_gone_404(client: TestClient) -> None:
    # DELETE 硬删除会话行（chunks 级联删除、分片目录清空），之后明确报 404。
    data = b"a" * 100
    sid = _create(client, data)
    _put(client, sid, 0, 100, data).raise_for_status()
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 204
    r = _audit(client, sid, [_manifest(data, 0, 100)])
    assert r.status_code == 404
    assert not storage.parts_dir(sid).exists()


# ---------------------------------------------------------------------------
# 并发：诊断全程持会话锁，与 repair / complete / abort 互斥
# ---------------------------------------------------------------------------


def test_audit_blocks_repair_for_whole_scan_and_never_reports_torn_state(
    client: TestClient, monkeypatch
) -> None:
    """诊断在第一片核对期间持锁时，repair 必须排队等待；

    诊断看到的要么全是旧内容、要么全是新内容，绝不出现同一次诊断中
    新旧混杂的“半替换”结果。"""
    import app.storage as storage_mod

    data = b"0" * 500 + b"1" * 500
    sid = _create(client, data)
    _put(client, sid, 0, 500, data[:500]).raise_for_status()
    _put(client, sid, 500, 1000, data[500:]).raise_for_status()
    items = [_manifest(data[:500], 0, 500), _manifest(data[500:], 500, 1000)]

    reached = threading.Event()
    release = threading.Event()
    used = {"hit": False}
    real_sha = storage_mod.sha256_file

    def paused_sha(path):
        result = real_sha(path)
        if not used["hit"]:
            used["hit"] = True
            reached.set()
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(storage_mod, "sha256_file", paused_sha)

    outcome: dict[str, int] = {}

    def do_audit() -> None:
        outcome["audit"] = _audit(client, sid, items).status_code

    t = threading.Thread(target=do_audit, daemon=True)
    t.start()
    assert reached.wait(timeout=10), "audit did not reach scanning stage"

    # 诊断持锁扫到第一片时发起 repair：必须阻塞到诊断结束。
    repair_done = threading.Event()

    def do_repair() -> None:
        r = client.put(
            f"/api/v1/sessions/{sid}/chunks/repair?start=0&end=500",
            content=b"A" * 500,
        )
        outcome["repair"] = r.status_code
        repair_done.set()

    tr = threading.Thread(target=do_repair, daemon=True)
    tr.start()
    assert not repair_done.wait(timeout=1.0), "repair ran while audit still held the lock"

    # 放行诊断：它完成时 repair 尚未落盘，因此两片都按旧内容核对为 ok。
    release.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert outcome["audit"] == 200
    assert repair_done.wait(timeout=10)
    tr.join(timeout=10)
    assert outcome["repair"] == 200

    # repair 落盘后再次诊断：第一片摘要不符（报告新内容摘要），第二片仍 ok。
    monkeypatch.undo()
    r = _audit(client, sid, items)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["status"] == "digest_mismatch"
    assert results[0]["actual_sha256"] == hashlib.sha256(b"A" * 500).hexdigest()
    assert results[1]["status"] == "ok"


def test_audit_concurrent_with_complete_gets_consistent_outcome(
    client: TestClient, monkeypatch
) -> None:
    """诊断与 complete 竞争：要么诊断先跑完（200 且随后 complete 成功），

    要么 complete 先提交（诊断看到 completed → 409）；不存在误报。"""
    import app.storage as storage_mod

    data = bytes((i * 5 + 1) % 256 for i in range(1000))
    sid = _create(client, data)
    _put(client, sid, 0, 1000, data).raise_for_status()
    items = [_manifest(data, 0, 1000)]

    reached = threading.Event()
    release = threading.Event()
    used = {"hit": False}
    real_sha = storage_mod.sha256_file

    def paused_sha(path):
        result = real_sha(path)
        if not used["hit"]:
            used["hit"] = True
            reached.set()
            release.wait(timeout=10)
        return result

    monkeypatch.setattr(storage_mod, "sha256_file", paused_sha)

    outcome: dict[str, int] = {}

    def do_audit() -> None:
        outcome["audit"] = _audit(client, sid, items).status_code

    t = threading.Thread(target=do_audit, daemon=True)
    t.start()
    assert reached.wait(timeout=10)

    # complete 同样需要会话锁：在诊断挂起期间只能排队。
    complete_done = threading.Event()

    def do_complete() -> None:
        outcome["complete"] = client.post(
            f"/api/v1/sessions/{sid}/complete"
        ).status_code
        complete_done.set()

    tc = threading.Thread(target=do_complete, daemon=True)
    tc.start()
    assert not complete_done.wait(timeout=1.0)

    release.set()
    t.join(timeout=10)
    assert complete_done.wait(timeout=10)
    tc.join(timeout=10)

    # 诊断必定先于 complete 拿到锁结果：audit=200(ok)，complete=200。
    assert outcome == {"audit": 200, "complete": 200}, outcome
    assert client.get(f"/api/v1/sessions/{sid}/download").content == data
