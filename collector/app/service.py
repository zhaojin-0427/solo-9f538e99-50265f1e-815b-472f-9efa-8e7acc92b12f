"""采集逻辑:先固定源站代际与目标版,再逐版补齐;失败保留原供数版。

代际(generation)规则:
- 每次采集先通过 /api/revisions/current 固定 (generation, current_version)。
- 代际不变:增量版只叠加同代际快照(prev+1 .. pinned 逐版补齐)。
- 代际变化(源站重置/重启):从新代际首版(v1,全量)重建,绝不叠加旧代际
  快照;新代际首版确认后,当前指针才切换到新代际,此前默认查询仍提供原供数。
- 分页期间每一页都校验 generation;代际一旦变化,本次采集失败,重试整版
  重取(不沿用残页),原完整供数不受影响。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

import httpx

from . import db as dbmod

SOURCE_BASE_URL = os.environ.get("SOURCE_BASE_URL", "http://localhost:8001")
PAGE_TIMEOUT = float(os.environ.get("PAGE_TIMEOUT_SECONDS", "3"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF_SECONDS", "0.5"))


class CollectError(Exception):
    """采集过程中可重试/可降级的错误。"""


class MissingPage(CollectError):
    """缺页。"""


class PageTimeout(CollectError):
    """超时。"""


class VersionConflict(CollectError):
    """同版同站冲突。"""


class GenerationChanged(CollectError):
    """分页期间源站代际发生变化。"""


class SourceError(CollectError):
    """源站响应异常。"""


class HourBusy(Exception):
    """同一小时已有采集在进行。"""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(hour: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(hour, threading.Lock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- 源站访问 ----------------
def _get_json(client: httpx.Client, path: str) -> dict:
    try:
        resp = client.get(path)
    except httpx.TimeoutException as e:
        raise PageTimeout(f"timeout fetching {path}") from e
    except httpx.HTTPError as e:
        raise SourceError(f"transport error fetching {path}: {e}") from e
    if resp.status_code == 404:
        raise MissingPage(f"missing page: {path}")
    if resp.status_code != 200:
        raise SourceError(f"source returned {resp.status_code} for {path}")
    try:
        return resp.json()
    except ValueError as e:
        raise SourceError(f"invalid JSON from {path}") from e


def _check_envelope(pg: dict, generation: str, version: int, page: int) -> None:
    if not isinstance(pg, dict):
        raise SourceError(f"version {version} page {page}: response is not an object")
    if pg.get("generation") != generation:
        raise GenerationChanged(
            f"version {version} page {page}: generation {pg.get('generation')!r} "
            f"!= pinned {generation!r}"
        )
    if pg.get("version") != version:
        raise SourceError(f"page {page}: version mismatch {pg.get('version')!r} != {version}")
    if pg.get("page") != page:
        raise SourceError(f"page mismatch: got {pg.get('page')!r}, want {page}")
    total = pg.get("total_pages")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        raise SourceError(f"page {page}: bad total_pages {total!r}")
    if not isinstance(pg.get("items"), list):
        raise SourceError(f"page {page}: items is not a list")


def _validate_item(it: object, version: int) -> tuple[str, str, float | None]:
    if not isinstance(it, dict):
        raise SourceError(f"version {version}: item is not an object")
    sid = it.get("station_id")
    op = it.get("op")
    if not isinstance(sid, str) or not sid:
        raise SourceError(f"version {version}: bad station_id {sid!r}")
    if op not in ("upsert", "delete"):
        raise SourceError(f"version {version} station {sid}: bad op {op!r}")
    if op == "upsert":
        pm = it.get("pm25")
        if not isinstance(pm, (int, float)) or isinstance(pm, bool) or pm < 0:
            raise SourceError(f"version {version} station {sid}: bad pm25 {pm!r}")
        return sid, "upsert", float(pm)
    return sid, "delete", None


def _fetch_version(client: httpx.Client, generation: str, version: int) -> tuple[int, list[dict]]:
    """整版抓取:每次调用都从第 1 页重新取,绝不沿用上次尝试的残页。

    每页都校验代际,分页期间源站重置(代际切换)会立即失败并整版重试。
    """
    first = _get_json(client, f"/api/revisions/{version}/pages/1")
    _check_envelope(first, generation, version, 1)
    total = first["total_pages"]
    raw = list(first["items"])
    for page in range(2, total + 1):
        pg = _get_json(client, f"/api/revisions/{version}/pages/{page}")
        _check_envelope(pg, generation, version, page)
        if pg["total_pages"] != total:
            raise SourceError(f"version {version}: total_pages changed between pages")
        raw.extend(pg["items"])
    # 同版同站冲突检测(完全相同的重复记录视为幂等,去噪)
    seen: dict[str, tuple] = {}
    items: list[dict] = []
    for it in raw:
        sid, op, pm = _validate_item(it, version)
        if sid in seen:
            if seen[sid] != (op, pm):
                raise VersionConflict(
                    f"version {version} station {sid}: conflicting entries in same version"
                )
            continue
        seen[sid] = (op, pm)
        items.append({"station_id": sid, "op": op, "pm25": pm})
    return total, items


def _apply(base: dict[str, float], items: list[dict]) -> dict[str, float]:
    snap = dict(base)
    for it in items:
        if it["op"] == "upsert":
            snap[it["station_id"]] = it["pm25"]
        else:
            snap.pop(it["station_id"], None)
    return snap


# ---------------- 采集主流程 ----------------
def collect_hour(hour: str) -> dict:
    lock = _lock_for(hour)
    if not lock.acquire(blocking=False):
        raise HourBusy(hour)
    try:
        return _collect(hour)
    finally:
        lock.release()


def _mark(db, hour: str, status: str, last_error: str | None = None) -> None:
    # 仅更新状态元信息,绝不动 current_generation/current_version 指针
    db.execute(
        "UPDATE hours SET status=?, last_error=?, updated_at=? WHERE hour=?",
        (status, last_error, _now(), hour),
    )


def _summary(hour, status, pinned_generation, pinned_version, new_generation,
             confirmed_from, confirmed_to, current_generation, current_version,
             error, attempts) -> dict:
    return {
        "hour": hour,
        "status": status,
        "pinned_generation": pinned_generation,
        "pinned_version": pinned_version,
        "new_generation": new_generation,  # 本次采集是否以新代际全量重建为目标
        "confirmed_from": confirmed_from,
        "confirmed_to": confirmed_to,
        "current_generation": current_generation,  # 采集结束后的当前指针(代际+版号)
        "current_version": current_version,
        "attempts": {str(v): n for v, n in attempts.items()},
        "error": error,
    }


def _collect(hour: str) -> dict:
    db = dbmod.connect()
    attempts: dict[int, int] = {}
    try:
        dbmod.ensure_hour(db, hour, _now())
        row = dbmod.get_hour(db, hour)
        cur_gen = row["current_generation"]
        cur_ver = row["current_version"]

        with httpx.Client(base_url=SOURCE_BASE_URL, timeout=PAGE_TIMEOUT) as client:
            # 1) 先固定源站代际与目标版
            try:
                pin = _get_json(client, "/api/revisions/current")
                pinned_gen = pin["generation"]
                if not isinstance(pinned_gen, str) or not pinned_gen:
                    raise SourceError(f"bad generation {pinned_gen!r}")
                pinned_ver = int(pin["current_version"])
            except (CollectError, KeyError, TypeError, ValueError) as e:
                msg = f"pin current revision failed: {e}"
                _mark(db, hour, "failed", msg)
                return _summary(hour, "failed", None, None, False,
                                cur_ver, cur_ver, cur_gen, cur_ver, msg, attempts)

            db.execute(
                "UPDATE hours SET status='collecting', pinned_generation=?, pinned_version=?, "
                "last_error=NULL, updated_at=? WHERE hour=?",
                (pinned_gen, pinned_ver, _now(), hour),
            )

            same_gen = cur_gen is not None and pinned_gen == cur_gen
            if same_gen:
                if pinned_ver < cur_ver:
                    msg = (f"source current {pinned_ver} regressed below confirmed {cur_ver} "
                           f"(generation {pinned_gen})")
                    _mark(db, hour, "failed", msg)
                    return _summary(hour, "source_regressed", pinned_gen, pinned_ver, False,
                                    cur_ver, cur_ver, cur_gen, cur_ver, msg, attempts)
                if pinned_ver == cur_ver:
                    _mark(db, hour, "ok")
                    return _summary(hour, "up_to_date", pinned_gen, pinned_ver, False,
                                    cur_ver, cur_ver, cur_gen, cur_ver, None, attempts)
                start = cur_ver + 1
            else:
                # 代际变化(或首次采集):从新代际首版全量重建,不叠加旧代际快照
                start = 1

            # 2) 逐版补齐:start .. pinned(同代际增量,或新代际全量重建)
            confirmed = cur_ver if same_gen else 0
            for version in range(start, pinned_ver + 1):
                # 首版为全量快照;后续版只叠加同代际前一版快照
                base = {} if version == 1 else dbmod.snapshot(db, hour, pinned_gen, version - 1)
                last_err: Exception | None = None
                ok = False
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        total, items = _fetch_version(client, pinned_gen, version)
                        new_snap = _apply(base, items)
                        # 3) 数据与当前指针(代际+版号)在同一事务生效;
                        #    新代际首版在此事务确认后,默认查询才切换到新代际
                        with dbmod.tx(db):
                            db.execute(
                                "INSERT INTO versions(hour, generation, version, total_pages, "
                                "item_count, station_count, confirmed_at) VALUES (?,?,?,?,?,?,?)",
                                (hour, pinned_gen, version, total, len(items),
                                 len(new_snap), _now()),
                            )
                            db.executemany(
                                "INSERT INTO entries(hour, generation, version, station_id, pm25) "
                                "VALUES (?,?,?,?,?)",
                                [(hour, pinned_gen, version, sid, pm)
                                 for sid, pm in sorted(new_snap.items())],
                            )
                            db.execute(
                                "UPDATE hours SET current_generation=?, current_version=?, "
                                "updated_at=? WHERE hour=?",
                                (pinned_gen, version, _now(), hour),
                            )
                        confirmed = version
                        attempts[version] = attempt
                        ok = True
                        break
                    except CollectError as e:
                        last_err = e
                        if attempt < MAX_ATTEMPTS:
                            time.sleep(RETRY_BACKOFF * attempt)
                if not ok:
                    # 缺页/超时/冲突/代际变化:保留原供数,指针不动,停止后续版本
                    msg = (f"generation {pinned_gen} version {version} failed after "
                           f"{MAX_ATTEMPTS} attempts: {last_err}")
                    _mark(db, hour, "failed", msg)
                    if same_gen or confirmed > 0:
                        # 同代际部分确认,或新代际首版已确认:指针在本代际已确认版
                        end_gen, end_ver = (cur_gen, confirmed) if same_gen \
                            else (pinned_gen, confirmed)
                    else:
                        # 新代际首版未确认:指针仍停留在旧代际,默认查询继续提供原供数
                        end_gen, end_ver = cur_gen, cur_ver
                    return _summary(hour, "failed", pinned_gen, pinned_ver, not same_gen,
                                    cur_ver if same_gen else 0, confirmed,
                                    end_gen, end_ver, msg, attempts)

            _mark(db, hour, "ok")
            end_gen = cur_gen if same_gen else pinned_gen
            return _summary(hour, "ok", pinned_gen, pinned_ver, not same_gen,
                            cur_ver if same_gen else 0, confirmed,
                            end_gen, confirmed, None, attempts)
    finally:
        db.close()
