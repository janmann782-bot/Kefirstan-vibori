from __future__ import annotations

import math
import random
import time
from collections import defaultdict

import db
from config import (
    BASE_SUPPORT, DISTRICT_SEATS, INDEPENDENT_DISTRICT_BASE, LIST_SEATS,
    MODEL_ELECTORATE, PARTIES, REGIONAL_EXCEPTION_HOME_SHARE, SIGNAL_LOGIT_SCALE,
    TURNOUT_RANGES, UNION_THRESHOLD,
)

ACTIVE_REGIONS = ("KAR", "TAR", "MAR")


def normalize(d: dict[str, float]) -> dict[str, float]:
    vals = {k: max(0.000001, float(v)) for k, v in d.items()}
    s = sum(vals.values()) or 1.0
    return {k: v / s for k, v in vals.items()}


def current_support(region: str, signals: dict[str, float], seed: int = 2059) -> dict[str, float]:
    # Маленький фиксированный дрейф делает 3 автономии не стерильными копиями базовых процентов.
    rng = random.Random(seed * 100 + sum(map(ord, region)))
    raw: dict[str, float] = {}
    for code in PARTIES:
        base = max(0.001, BASE_SUPPORT[region].get(code, 0.001))
        drift = rng.uniform(-0.025, 0.025)
        sig = float(signals.get(code, 0.0))
        raw[code] = base * math.exp(drift + SIGNAL_LOGIT_SCALE * sig)
    return normalize(raw)


def allocate_expected(delta: int, support: dict[str, float], seed: int) -> dict[str, int]:
    if delta <= 0:
        return {p: 0 for p in PARTIES}
    expected = {p: delta * support[p] for p in PARTIES}
    out = {p: int(math.floor(v)) for p, v in expected.items()}
    left = delta - sum(out.values())
    frac = [(expected[p] - out[p], p) for p in PARTIES]
    rng = random.Random(seed)
    # Доли с большей дробной частью обычно получают остаток, но порядок чуть шевелится.
    frac.sort(key=lambda x: (x[0] + rng.random() * 0.08), reverse=True)
    for _, p in frac[:left]:
        out[p] += 1
    return out


async def open_election(duration_seconds: int, test_mode: bool = False) -> None:
    await db.reset_election_data()
    now = time.time()
    seed = random.SystemRandom().randint(205900, 205999999)
    for region in ACTIVE_REGIONS:
        low, high = TURNOUT_RANGES[region]
        turnout = random.SystemRandom().uniform(low, high)
        await db.set_turnout_target(region, turnout)
    await db.set_election(
        status="active", start_ts=now, end_ts=now + duration_seconds,
        seed=seed, last_tick_ts=now, snapshot_no=0, test_mode=int(test_mode),
    )


async def close_election() -> None:
    await advance(force_final=True)
    await db.set_election(status="closed", last_tick_ts=time.time())


async def advance(force_final: bool = False) -> bool:
    election = await db.get_election()
    if election["status"] != "active":
        return False
    now = time.time()
    start = float(election["start_ts"] or now)
    end = float(election["end_ts"] or now)
    duration = max(1.0, end - start)
    progress = 1.0 if force_final else max(0.0, min(1.0, (now - start) / duration))
    # Первые часы идут живее, потом поток немного ровнее. На 100% времени получаем весь target.
    curve = 1.0 - (1.0 - progress) ** 1.18

    region_rows = await db.get_region_rows()
    all_signals = await db.get_signals()
    snapshot = int(election["snapshot_no"] or 0)
    any_change = False

    for region in ACTIVE_REGIONS:
        row = region_rows[region]
        target_total = int(row["electorate"] * row["turnout_target"] * curve)
        delta = max(0, target_total - int(row["counted"]))
        if delta <= 0:
            continue
        support = current_support(region, all_signals[region], int(election["seed"]))
        additions = allocate_expected(delta, support, int(election["seed"]) + snapshot * 31 + sum(map(ord, region)))
        await db.add_counts(region, additions)
        any_change = True

    await db.set_election(last_tick_ts=now)
    if now >= end and not force_final:
        await close_election()
        return True
    return any_change


def shares_from_counts(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {p: 0.0 for p in PARTIES}
    return {p: counts[p] / total for p in PARTIES}


def dhondt(votes: dict[str, float], seats: int, eligible: set[str]) -> dict[str, int]:
    alloc = {p: 0 for p in votes}
    for _ in range(seats):
        best = None
        best_q = -1.0
        for p in eligible:
            q = votes.get(p, 0.0) / (alloc[p] + 1)
            if q > best_q:
                best_q, best = q, p
        if best is None:
            break
        alloc[best] += 1
    return alloc


def distribute_region_seats(total_seats: int) -> dict[str, int]:
    total_pop = sum(MODEL_ELECTORATE.values())
    raw = {r: total_seats * MODEL_ELECTORATE[r] / total_pop for r in ACTIVE_REGIONS}
    out = {r: int(math.floor(v)) for r, v in raw.items()}
    left = total_seats - sum(out.values())
    for r in sorted(ACTIVE_REGIONS, key=lambda x: raw[x] - out[x], reverse=True)[:left]:
        out[r] += 1
    return out


def project_seats(counts: dict[str, dict[str, int]], seed: int = 2059) -> dict[str, int]:
    # 180 мест - партийные списки внутри автономий. 60 - одномандатные округа.
    result = {p: 0 for p in PARTIES}
    result["IND"] = 0
    national = {p: 0 for p in PARTIES}
    total = 0
    shares_by_region = {}
    for r in ACTIVE_REGIONS:
        shares_by_region[r] = shares_from_counts(counts[r])
        total_r = sum(counts[r].values())
        total += total_r
        for p in PARTIES:
            national[p] += counts[r][p]

    national_share = {p: (national[p] / total if total else 0) for p in PARTIES}
    list_by_region = distribute_region_seats(LIST_SEATS)
    smd_by_region = distribute_region_seats(DISTRICT_SEATS)

    for r in ACTIVE_REGIONS:
        eligible = {p for p in PARTIES if national_share[p] >= UNION_THRESHOLD}
        for p, party in PARTIES.items():
            if party.home_region == r and shares_by_region[r].get(p, 0) >= REGIONAL_EXCEPTION_HOME_SHARE:
                eligible.add(p)
        alloc = dhondt({p: counts[r][p] for p in PARTIES}, list_by_region[r], eligible)
        for p, s in alloc.items():
            result[p] += s

        # Округа: строим 60 маленьких локальных гонок вокруг текущей поддержки.
        rng = random.Random(seed + sum(map(ord, r)) * 17)
        party_share = shares_by_region[r]
        for _ in range(smd_by_region[r]):
            candidates = {p: max(0.0001, party_share[p] * rng.uniform(0.78, 1.22)) for p in PARTIES}
            candidates["IND"] = INDEPENDENT_DISTRICT_BASE[r] * rng.uniform(0.70, 1.35)
            winner = max(candidates, key=candidates.get)
            result[winner] += 1
    return result


async def snapshot() -> dict:
    election = await db.get_election()
    counts = await db.get_counts()
    region_rows = await db.get_region_rows()
    signals = await db.get_signals()

    shares = {r: shares_from_counts(counts[r]) for r in ACTIVE_REGIONS}
    # До открытия выборов карта все равно показывает фон кампании, а не нули.
    if sum(sum(v.values()) for v in counts.values()) == 0:
        shares = {r: current_support(r, signals[r], int(election["seed"] or 2059)) for r in ACTIVE_REGIONS}

    winners = {r: max(PARTIES, key=lambda p: shares[r][p]) for r in ACTIVE_REGIONS}
    seats = project_seats(counts, int(election["seed"] or 2059)) if sum(sum(v.values()) for v in counts.values()) else None

    start = float(election["start_ts"] or 0)
    end = float(election["end_ts"] or 0)
    now = time.time()
    if election["status"] == "active" and end > start:
        progress = max(0.0, min(1.0, (now - start) / (end - start)))
    elif start and end and now >= end:
        progress = 1.0
    else:
        progress = 0.0

    return {
        "election": election,
        "counts": counts,
        "shares": shares,
        "winners": winners,
        "regions": region_rows,
        "seats": seats,
        "progress": progress,
        "now": now,
    }
