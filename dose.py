# Dose formula, safety limits and caf/decaf split; every number lives in config.json.
import datetime as dt
import math

STRENGTHS = ["mild", "regular", "strong"]
SPOKEN = {"mild": "light", "regular": "medium-strength", "strong": "dark"}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def clock(t):
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}".replace(":00 ", " ")


def day_summary(events, now, cfg):
    # What the briefing and formula need from today's calendar.
    timed = sorted([e for e in events if not e["all_day"]], key=lambda e: e["start"])
    important = [e for e in events if e["flagged"] or any(k in e["title"].lower() for k in cfg["important_keywords"])]
    window = now + dt.timedelta(hours=cfg["formula"]["important_window_h"])
    soon = next((e for e in sorted(important, key=lambda e: e["start"]) if not e["all_day"] and e["start"] <= window), None)
    first, last_end = (timed[0], max(e["end"] for e in timed)) if timed else (None, None)
    span = (last_end - max(first["start"], now)).total_seconds() / 3600 if timed else 0
    return {"first": first, "important": important, "important_soon": soon, "last_end": last_end,
            "span_h": round(span, 1), "count": len(timed)}


def sleep_quality(h):
    # 0-100: 40 % efficiency (75->95 %), 30 % deep (18 % of sleep), 30 % REM (22 %); re-weighted if parts missing.
    parts, asleep = [], (h.get("sleep_hours") or 0) * 60
    if h.get("sleep_efficiency_pct") is not None:
        parts.append((0.4, clamp((h["sleep_efficiency_pct"] - 75) / 20, 0, 1)))
    for key, w, share in (("deep_min", 0.3, 0.18), ("rem_min", 0.3, 0.22)):
        if asleep and h.get(key) is not None:
            parts.append((w, clamp(h[key] / asleep / share, 0, 1)))
    return round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts)) if parts else None


def readiness(h, q):
    # Our own 0-100 readiness (Fitbit doesn't share theirs).
    terms = [(0.6 * clamp(h["hrv_delta_pct"], -40, 40)) if h.get("hrv_delta_pct") is not None else None,
             (-3 * h["rhr_delta"]) if h.get("rhr_delta") is not None else None,
             (4 * (h["sleep_hours"] - 7)) if h.get("sleep_hours") is not None else None,
             (0.25 * (q - 70)) if q is not None else None]
    terms = [t for t in terms if t is not None]
    return int(clamp(round(70 + sum(terms)), 1, 100)) if terms else None


def recommend(h, day, cfg, today):
    # Returns (mg, [(reason, change)], extras).
    f, mg, reasons = cfg["formula"], float(cfg["formula"]["base_mg"]), []

    def add(text, d):
        nonlocal mg
        if abs(d) >= 0.5:
            mg += d
            reasons.append((text, round(d, 1)))

    if h.get("sleep_hours") is not None:
        add(f"sleep debt ({h['sleep_hours']:.1f} h)", min(f["max_sleep_debt_mg"], f["mg_per_hour_debt"] * max(0, f["sleep_target_h"] - h["sleep_hours"])))
    q = sleep_quality(h)
    if q is not None:
        add(f"sleep quality {q}", clamp((70 - q) * f["mg_per_quality_point"], -10, 20))
    strain = f["mg_per_bpm"] * max(0, (h.get("rhr_delta") or 0) - 2) + f["mg_per_hrv_pct"] * max(0, -(h.get("hrv_delta_pct") or 0) - 5)
    add("body under strain", -min(f["max_strain_mg"], strain))
    if day["important_soon"]:
        add(f"{day['important_soon']['title']} soon", f["important_event_mg"])
    if day["span_h"] > f["long_day_h"]:
        add(f"long day ({day['span_h']:.0f} h)", min(f["long_day_max_mg"], f["mg_per_long_day_h"] * (day["span_h"] - f["long_day_h"])))
    illness = sum([(h.get("skin_temp_dev") or 0) >= 0.5, (h.get("breath_rate_delta") or 0) >= 1.5,
                   h.get("spo2_avg") is not None and h["spo2_avg"] < 94]) >= 2
    if illness:
        add("possible illness signs", -f["illness_mg"])
    if cfg.get("taper_enabled"):
        weeks = max(0, (today - dt.date.fromisoformat(cfg["taper_start_date"])).days // 7)
        add(f"tapering, week {weeks}", mg * max(0.0, 1 - cfg["taper_pct_per_week"] / 100 * weeks) - mg)
    return max(0, round(mg)), reasons, {"quality": q, "readiness": readiness(h, q), "illness": illness}


def adjust_for_feeling(mg, feeling, cfg):
    return max(0, round(mg + (3 - feeling) * cfg["formula"]["mg_per_feeling_step"] if feeling else mg))


def bedtime_cap(cfg, now, bed, earlier):
    # Most caffeine this cup can hold so at most max_mg_at_bedtime is left at bedtime (caffeine halves every half-life).
    hl = cfg["caffeine_half_life_h"]
    left = sum(mg * 0.5 ** ((bed - t).total_seconds() / 3600 / hl) for t, mg in earlier)
    return max(0.0, cfg["max_mg_at_bedtime"] - left) * 2 ** ((bed - now).total_seconds() / 3600 / hl)


def limit(mg, cfg, now, used_mg, drinks=0, bed=None, earlier=()):
    # Smallest limit wins: per cup, per day (Health Canada), and bedtime (or plain afternoon cap). Returns (allowed, cap, why).
    day_max = cfg["max_day_mg_pregnant"] if cfg.get("pregnancy_mode") else cfg["max_day_mg"]
    used = used_mg + drinks * cfg["mg_per_reported_drink"]
    caps = [(cfg["max_cup_mg"], f"{cfg['max_cup_mg']} milligrams is the most for one cup"),
            (max(0, day_max - used), f"you've had about {used:.0f} milligrams today, and {day_max} is the daily limit")]
    if bed is not None:
        caps.append((bedtime_cap(cfg, now, bed, earlier), f"you're going to bed at {clock(bed)}, and it needs to wear off by then"))
    elif now.hour >= cfg["afternoon_cutoff_hour"]:
        caps.append((cfg["afternoon_max_mg"], "it's afternoon, so I keep it light for your sleep"))
    cap, why = min(caps, key=lambda c: c[0])
    return max(0, min(mg, cap)), cap, (why if mg > cap else None)


def split(dose_mg, strength, cfg, cap_mg):
    # Solve Vc + Vd = V, Cc*Vc + Cd*Vd = dose; returns (caf_ml, decaf_ml, actual_mg, water_ml, stretched).
    Cc, Cd, step = cfg["caf_mg_per_ml"], cfg["decaf_mg_per_ml"], cfg["pump_step_ml"]
    V, stretched = cfg["strength_ml"][strength], dose_mg > Cc * cfg["strength_ml"][strength]
    if stretched:
        V = min(cfg["max_concentrate_ml"], math.ceil(dose_mg / Cc / step) * step)
    vc = V if dose_mg >= Cc * V else 0.0 if dose_mg <= Cd * V else round((dose_mg - Cd * V) / (Cc - Cd) / step) * step
    actual = lambda v: Cc * v + Cd * (V - v)
    while vc > 0 and actual(vc) > cap_mg + 1e-9:  # rounding never passes the limit
        vc = math.floor(vc / step - 1e-9) * step
    vc = round(clamp(vc, 0.0, V), 2)
    return vc, round(V - vc, 2), round(actual(vc), 1), round(cfg["cup_ml"] - V, 1), stretched
