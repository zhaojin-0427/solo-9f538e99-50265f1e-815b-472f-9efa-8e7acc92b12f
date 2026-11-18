#!/usr/bin/env python3
"""端到端冒烟测试:对运行中的 source + collector 验证全部关键行为。

用法:
    python3 scripts/smoke.py                 # 默认打 http://localhost:8000 / 8001
    COLLECTOR_URL=... SOURCE_URL=... python3 scripts/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://localhost:8000").rstrip("/")
SOURCE = os.environ.get("SOURCE_URL", "http://localhost:8001").rstrip("/")
HOUR = os.environ.get("SMOKE_HOUR", "2026-11-18T10")

_checks = 0


def req(method: str, base: str, path: str, body=None, expect: int | None = 200):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {url} -> {code}, want {expect}: {payload[:400]!r}")
    return code, (json.loads(payload) if payload else {})


def check(name: str, cond: bool, extra: str = "") -> None:
    global _checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    _checks += 1
    print(f"  ok [{_checks:02d}] {name}")


def stations(hour: str, generation: str | None = None, version: int | None = None) -> dict:
    q = []
    if generation is not None:
        q.append(f"generation={generation}")
    if version is not None:
        q.append(f"version={version}")
    _, data = req("GET", COLLECTOR, f"/hours/{hour}/data{'?' + '&'.join(q) if q else ''}")
    return {s["station_id"]: s["pm25"] for s in data["stations"]}


def collect(hour: str) -> dict:
    _, r = req("POST", COLLECTOR, "/collections", {"hour": hour})
    return r


def source_generation() -> str:
    _, cur = req("GET", SOURCE, "/api/revisions/current")
    return cur["generation"]


def main() -> int:
    print(f"== smoke: collector={COLLECTOR} source={SOURCE} hour={HOUR}")

    print("-- 0. 健康检查")
    req("GET", SOURCE, "/healthz")
    req("GET", COLLECTOR, "/healthz")

    print("-- 1. 重置源站(代际 A),采集首版(全量)")
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 20261118})
    gen_a = reset["generation"]
    check("reset -> current=1 且返回新代际", reset["current_version"] == 1 and bool(gen_a))
    r = collect(HOUR)
    check("采集 v1 ok(新代际全量重建)",
          r["status"] == "ok" and r["confirmed_to"] == 1 and r["new_generation"], json.dumps(r))
    check("采集固定的代际=源站代际", r["pinned_generation"] == gen_a, json.dumps(r))
    v1 = stations(HOUR)
    check("v1 全量 20 站", len(v1) == 20)
    check("默认查询=当前指针", stations(HOUR) == v1)

    print("-- 2. 源站发布 v2/v3(增量),逐版补齐")
    _, p2 = req("POST", SOURCE, "/admin/publish")
    _, p3 = req("POST", SOURCE, "/admin/publish")
    deleted_v2 = [o["station_id"] for o in p2["ops"] if o["op"] == "delete"]
    r = collect(HOUR)
    check("补齐到 v3", r["status"] == "ok" and r["confirmed_from"] == 1 and r["confirmed_to"] == 3,
          json.dumps(r))
    check("同代际增量不视为新代际", not r["new_generation"], json.dumps(r))
    v2, v3 = stations(HOUR, version=2), stations(HOUR, version=3)
    check("v2 删除了目标站", all(s not in v2 for s in deleted_v2) and deleted_v2,
          f"deleted={deleted_v2}")
    check("v1 仍含被删站(已确认版均可查当时数据)", all(s in v1 for s in deleted_v2))
    check("v3 含新站 N03", "N03" in v3 and "N03" not in v1)
    check("v1 数据未被后续版本污染", stations(HOUR, version=1) == v1)

    print("-- 3. 重复采集:已是最新(同代际同版)")
    r = collect(HOUR)
    check("up_to_date", r["status"] == "up_to_date" and r["current_version"] == 3, json.dumps(r))

    print("-- 4. 超时故障:一次性,重试后成功(重试不混残页)")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 4, "page": 1, "kind": "timeout", "times": 1}]})
    req("POST", SOURCE, "/admin/publish")
    r = collect(HOUR)
    check("v4 重试后成功", r["status"] == "ok" and r["confirmed_to"] == 4, json.dumps(r))
    check("v4 第 2 次尝试才成功", r["attempts"].get("4") == 2, json.dumps(r["attempts"]))

    print("-- 5. 缺页故障:持续缺页 -> 保留原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 5, "page": 1, "kind": "missing", "times": -1}]})
    req("POST", SOURCE, "/admin/publish")
    r = collect(HOUR)
    check("v5 缺页采集失败", r["status"] == "failed" and "missing page" in (r["error"] or ""),
          json.dumps(r))
    check("指针保留在 v4", r["current_version"] == 4, json.dumps(r))
    _, cur = req("GET", COLLECTOR, f"/hours/{HOUR}/data")
    check("默认查询仍是 v4(未暴露半版)", cur["version"] == 4 and cur["is_current"])
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=5", expect=404)
    check("v5 不可查", True)
    req("DELETE", SOURCE, "/admin/faults")
    r = collect(HOUR)
    check("清障后补齐 v5", r["status"] == "ok" and r["confirmed_to"] == 5, json.dumps(r))

    print("-- 6. 同版同站冲突:保留原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 6, "page": 1, "kind": "conflict", "times": -1}]})
    req("POST", SOURCE, "/admin/publish")
    r = collect(HOUR)
    check("v6 冲突采集失败", r["status"] == "failed" and "conflict" in (r["error"] or "").lower(),
          json.dumps(r))
    check("指针保留在 v5", r["current_version"] == 5, json.dumps(r))
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=6", expect=404)
    check("v6 不可查", True)
    req("DELETE", SOURCE, "/admin/faults")
    r = collect(HOUR)
    check("清障后补齐 v6", r["status"] == "ok" and r["confirmed_to"] == 6, json.dumps(r))

    print("-- 7. 已确认版完整性:逐版快照与版本登记一致")
    _, vers = req("GET", COLLECTOR, f"/hours/{HOUR}/versions")
    vers_a = [v for v in vers["versions"] if v["generation"] == gen_a]
    check("代际 A 确认版 1..6 连续", [v["version"] for v in vers_a] == [1, 2, 3, 4, 5, 6])
    for v in vers_a:
        snap = stations(HOUR, generation=gen_a, version=v["version"])
        check(f"代际A v{v['version']} 快照站数={v['station_count']}", len(snap) == v["station_count"])
    check("当前指针在代际 A v6",
          vers["current_generation"] == gen_a and vers["current_version"] == 6)

    print("-- 8. 源站重置 -> 新代际 B:从首版全量重建,旧代际仍可查")
    v6_a = stations(HOUR, generation=gen_a, version=6)
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 999})
    gen_b = reset["generation"]
    check("重置产生新代际", gen_b != gen_a)
    r = collect(HOUR)
    check("同号 v1 不误报已最新/回退,而是全量重建",
          r["status"] == "ok" and r["new_generation"] and r["confirmed_from"] == 0
          and r["confirmed_to"] == 1, json.dumps(r))
    check("指针切换到代际 B v1",
          r["current_generation"] == gen_b and r["current_version"] == 1, json.dumps(r))
    v1_b = stations(HOUR)
    check("默认查询=代际 B v1(20 站)", len(v1_b) == 20)
    check("新代际数据与旧代际不同(不同种子)", v1_b != stations(HOUR, generation=gen_a, version=1))
    check("旧代际 A v6 仍可按代际+版号查询", stations(HOUR, generation=gen_a, version=6) == v6_a)
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=6", expect=404)
    check("当前代际 B 无 v6(同名版号不串代际)", True)

    print("-- 9. 再次重置 -> 代际 C:同为 v1 也不误报已最新")
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 777})
    gen_c = reset["generation"]
    r = collect(HOUR)
    check("双方都是 v1 仍触发重建(代际不同)",
          r["status"] == "ok" and r["new_generation"] and r["current_generation"] == gen_c,
          json.dumps(r))
    check("默认查询=代际 C v1", stations(HOUR) == stations(HOUR, generation=gen_c, version=1))

    print("-- 10. 新代际增量只叠加同代际快照")
    ops = [{"station_id": "S01", "op": "upsert", "pm25": 1.5},
           {"station_id": "S02", "op": "delete"},
           {"station_id": "NEW1", "op": "upsert", "pm25": 88.8}]
    _, pub = req("POST", SOURCE, "/admin/publish", {"ops": ops})
    check("发布落在代际 C", pub["generation"] == gen_c and pub["version"] == 2)
    r = collect(HOUR)
    check("补齐代际 C v2", r["status"] == "ok" and r["confirmed_to"] == 2
          and not r["new_generation"], json.dumps(r))
    v1_c = stations(HOUR, generation=gen_c, version=1)
    expected = dict(v1_c)
    expected["S01"] = 1.5
    del expected["S02"]
    expected["NEW1"] = 88.8
    v2_c = stations(HOUR, generation=gen_c, version=2)
    check("代际 C v2 == apply(代际 C v1, ops)(未叠旧代际快照)", v2_c == expected)
    check("代际 C v2 与代际 B 快照无关", v2_c != stations(HOUR, generation=gen_b, version=1))

    print("-- 11. 新代际首版失败:继续提供原完整供数")
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 555})
    gen_d = reset["generation"]
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 1, "page": 1, "kind": "missing", "times": -1}]})
    r = collect(HOUR)
    check("代际 D v1 缺页采集失败", r["status"] == "failed" and "missing page" in (r["error"] or ""),
          json.dumps(r))
    check("指针仍停留在代际 C v2",
          r["current_generation"] == gen_c and r["current_version"] == 2, json.dumps(r))
    _, cur = req("GET", COLLECTOR, f"/hours/{HOUR}/data")
    check("默认查询仍提供代际 C v2(原供数)",
          cur["generation"] == gen_c and cur["version"] == 2 and cur["is_current"])
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?generation={gen_d}&version=1", expect=404)
    check("代际 D 无任何可查版", True)
    req("DELETE", SOURCE, "/admin/faults")
    r = collect(HOUR)
    check("清障后重建代际 D,默认查询才切换",
          r["status"] == "ok" and r["current_generation"] == gen_d, json.dumps(r))
    _, cur = req("GET", COLLECTOR, f"/hours/{HOUR}/data")
    check("默认查询=代际 D v1", cur["generation"] == gen_d and cur["version"] == 1)

    print("-- 12. 分页期间代际变化:保留原供数,重试不沿用残页")
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 31337})
    gen_e = reset["generation"]
    # 采集代际 E v1(共 3 页)时,第 2 页触发源站重置 -> 代际 F
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 1, "page": 2, "kind": "reset", "seed": 4242, "times": 1}]})
    r = collect(HOUR)
    check("分页期间代际变化 -> 采集失败",
          r["status"] == "failed" and "generation" in (r["error"] or "").lower(), json.dumps(r))
    check("指针仍停留在代际 D v1",
          r["current_generation"] == gen_d and r["current_version"] == 1, json.dumps(r))
    _, cur = req("GET", COLLECTOR, f"/hours/{HOUR}/data")
    check("默认查询仍提供代际 D v1(残页未落库)",
          cur["generation"] == gen_d and cur["version"] == 1)
    gen_f = source_generation()
    check("源站已被故障重置到代际 F", gen_f != gen_e)
    r = collect(HOUR)
    check("再次采集:从代际 F 首版全量重建",
          r["status"] == "ok" and r["new_generation"] and r["current_generation"] == gen_f,
          json.dumps(r))
    check("代际 F v1 全量 20 站", len(stations(HOUR)) == 20)

    print("-- 13. 跨代际版本登记完整,旧代际快照全部可查")
    _, vers = req("GET", COLLECTOR, f"/hours/{HOUR}/versions")
    by_gen = {}
    for v in vers["versions"]:
        by_gen.setdefault(v["generation"], []).append(v["version"])
    check("代际 A/B/C/D/F 均有确认版",
          all(g in by_gen for g in (gen_a, gen_b, gen_c, gen_d, gen_f)),
          json.dumps({k[:8]: v for k, v in by_gen.items()}))
    check("代际 E 分页期间失败,无确认版", gen_e not in by_gen,
          json.dumps({k[:8]: v for k, v in by_gen.items()}))
    check("当前指针=代际 F v1",
          vers["current_generation"] == gen_f and vers["current_version"] == 1)
    for g, want in ((gen_a, 6), (gen_b, 1), (gen_c, 2), (gen_d, 1)):
        snap = stations(HOUR, generation=g, version=want)
        check(f"旧代际 {g[:8]} v{want} 仍完整可查({len(snap)} 站)", len(snap) > 0)
    _, st = req("GET", COLLECTOR, f"/collections/{HOUR}")
    check("采集状态接口携带代际指针",
          st["current_generation"] == gen_f and st["status"] == "ok", json.dumps(st))

    print(f"== smoke 全部通过 ({_checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
