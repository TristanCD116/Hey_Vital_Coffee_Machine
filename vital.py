# Vital: "Hey Vital" -> briefing from Google Health + Calendar -> questions -> caf/decaf/water to the Uno's pumps.
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import time

import google_data
from chat import LINES, Conversation, Gemini, brew_line, rec_question
from dose import SPOKEN, STRENGTHS, adjust_for_feeling, clock, day_summary, limit, recommend, split

HERE = os.path.dirname(os.path.abspath(__file__))


class CaffeineLog:
    # Today's caffeine survives a reboot, so the daily limit holds.
    def __init__(self, path):
        self.db, self.lk = sqlite3.connect(path, check_same_thread=False), threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS brews(ts TEXT, decaf_ml REAL, caf_ml REAL, mg REAL, strength TEXT)")

    def add(self, now, decaf, caf, mg, strength):
        with self.lk:
            self.db.execute("INSERT INTO brews VALUES (?,?,?,?,?)", (now.isoformat(), decaf, caf, mg, strength))
            self.db.commit()

    def today(self, now):
        with self.lk:
            return [(dt.datetime.fromisoformat(t), mg) for t, mg in self.db.execute("SELECT ts, mg FROM brews WHERE substr(ts,1,10)=?", (now.date().isoformat(),))]


def plan(cfg, log, data, now):
    # Everything known before the first question: the dose so far and the briefing facts (key = what would be said).
    health, herr, events, cerr = data
    events = [e for e in events if e["end"] > now]
    day = day_summary(events, now, cfg)
    rec_mg, reasons, x = recommend(health, day, cfg, now.date())
    earlier = [] if cfg.get("demo_mode") else log.today(now)  # demo: earlier cups don't count
    used = sum(mg for _, mg in earlier)
    sugg, cap, why = limit(rec_mg, cfg, now, used)
    first, soon, sh = day["first"], day["important_soon"], health.get("sleep_hours")
    brief_mg = round(split(sugg, cfg["default_strength"], cfg, cap)[2])
    facts = {
        "hour": now.hour, "name": cfg.get("user_name"), "sleep_hours": sh,
        "sleep_spoken": f"{int(sh)} hours {round(sh % 1 * 60)} minutes" if sh is not None else None,
        "sleep_efficiency_percent": health.get("sleep_efficiency_pct"), "deep_sleep_minutes": health.get("deep_min"),
        "rem_sleep_minutes": health.get("rem_min"), "sleep_quality_out_of_100": x["quality"], "readiness_out_of_100": x["readiness"],
        "resting_hr_change_bpm": health.get("rhr_delta"), "illness_signs": x["illness"],
        "first_event": f"{first['title']} at {clock(first['start'])}" if first else None,  # absolute times, so a pre-built briefing stays right
        "important_today": [e["title"] + ("" if e["all_day"] else f" at {clock(e['start'])}") for e in day["important"]] or None,
        "important_soon": soon["title"] if soon else None, "events_today": day["count"],
        "day_ends": clock(day["last_end"]) if day["last_end"] else None,
        "day_length_hours": day["span_h"] if day["span_h"] >= cfg["formula"]["long_day_h"] else None,
        "caffeine_already_today_milligrams": round(used), "problems": [p for p in (herr, cerr, why) if p]}
    return {"health": health, "events": events, "rec_mg": rec_mg, "reasons": reasons, "x": x, "used": used, "earlier": earlier,
            "sugg": sugg, "brief_mg": brief_mg, "facts": facts, "key": json.dumps([facts, brief_mg], sort_keys=True, default=str)}


class Prefetch:
    # Google data fetched in the background (every 10 min and after each cup); the briefing is written and recorded ahead.
    def __init__(self, cfg, log, conv):
        self.cfg, self.log, self.conv, self.brief, self.got, self.at = cfg, log, conv, None, None, 0.0
        self.lock, self.wake = threading.Lock(), threading.Event()
        threading.Thread(target=self._fetch_loop, daemon=True).start()
        if cfg.get("prebuild_briefing", True):
            threading.Thread(target=self._brief_loop, daemon=True).start()

    def _fetch(self):
        t = time.time()
        got = (*google_data.get_health(), *google_data.get_events(dt.datetime.now()))
        with self.lock:
            self.got, self.at = got, time.time()
        print(f"  [prefetch] Google data ready ({time.time() - t:.1f} s)")

    def _fetch_loop(self):
        while True:
            try:
                self._fetch()
            except Exception as e:
                print(f"  [prefetch] failed ({e})")
            self.wake.wait(self.cfg.get("prefetch_every_s", 600))
            self.wake.clear()

    def _brief_loop(self):
        # Re-plan every 30 s from cached data; Gemini and ElevenLabs are only called when what Vital would say changes.
        while True:
            try:
                now = dt.datetime.now().replace(microsecond=0)
                p = plan(self.cfg, self.log, self.got, now) if self.got else None
                if p and (not self.brief or self.brief[0] != p["key"]):
                    t, text = time.time(), self.conv.briefing_text(p["facts"], p["brief_mg"])
                    if self.conv.prerender(text):
                        self.brief = (p["key"], text)
                        print(f"  [prefetch] briefing recorded and ready ({time.time() - t:.1f} s)")
                    if self.cfg.get("prerender_recommendations", True):
                        for feeling in range(1, 6):  # the recommendation for each 1-5 answer at the default strength
                            allowed, cap, why = limit(adjust_for_feeling(p["rec_mg"], feeling, self.cfg), self.cfg, now, p["used"])
                            caf, decaf, actual, water, stretched = split(allowed, self.cfg["default_strength"], self.cfg, cap)
                            self.conv.prerender(rec_question(why, stretched, SPOKEN[self.cfg["default_strength"]], actual))
            except Exception as e:
                print(f"  [prefetch] briefing not pre-built ({e})")
            time.sleep(30)

    def get(self):
        # Cached data, fetched now if missing, stale (15 min) or partly failed.
        with self.lock:
            got, age = self.got, time.time() - self.at
        if got is None or age > self.cfg.get("prefetch_max_age_s", 900) or got[1] or got[3]:
            print("  [prefetch] no fresh data yet, fetching now")
            self._fetch()
        return self.got


def morning(cfg, log, conv, data, uno=None):
    t_start, now = time.time(), dt.datetime.now().replace(microsecond=0)
    p = plan(cfg, log, data.get(), now)
    print(f"  [data] health { {k: v for k, v in p['health'].items() if v is not None} or 'none'}")
    print(f"  [data] events {[(e['title'], 'all day' if e['all_day'] else e['start'].strftime('%H:%M')) for e in p['events']] or 'none'}")
    print(f"  [dose] {cfg['formula']['base_mg']} base " + " ".join(f"{d:+.1f} ({t})" for t, d in p["reasons"])
          + f" -> {p['rec_mg']} mg, after limits {p['sugg']:.0f} mg   (quality {p['x']['quality']}, readiness {p['x']['readiness']})")
    ready = data.brief[1] if data.brief and data.brief[0] == p["key"] else None
    conv.audio.say(ready or conv.briefing_text(p["facts"], p["brief_mg"]))
    print(f"  [time] Hey Vital -> briefing sound: {conv.audio.last_first_audio - t_start:.2f} s ({'pre-built' if ready else 'built just now'})")

    alert = conv.ask("scale").get("feeling")
    a = conv.ask("drinks")
    drinks = a.get("count", conv.ask("count").get("count") if a.get("yes") else 0) or 0
    bt, bed = conv.ask("bedtime").get("bedtime"), None
    if bt:
        bed = now.replace(hour=bt[0], minute=bt[1], second=0)
        bed += dt.timedelta(days=1) if bed <= now else dt.timedelta(0)  # "one AM" = tonight, after midnight
    strength = conv.ask("strength").get("strength") or cfg["default_strength"]

    mg, nos, changing = adjust_for_feeling(p["rec_mg"], alert, cfg), 0, False
    for _ in range(6):  # recommend, then let them change it
        allowed, cap, why = limit(mg, cfg, now, p["used"], drinks, bed, p["earlier"])
        caf, decaf, actual, water, stretched = split(allowed, strength, cfg, cap)
        conv.prerender(brew_line(SPOKEN[strength], actual, uno is not None), background=True)  # ready if they say yes
        ans = conv.ask("confirm", "What should I change? You can say more, less, lighter, darker, or cancel." if changing
                       else rec_question(why, stretched, SPOKEN[strength], actual))
        act, changing = ans.get("action"), False
        if act == "yes":
            break
        if act in (None, "cancel") or (act == "no" and nos >= 1):
            return conv.say(LINES["cancel"])
        nos, changing = (nos + 1, True) if act == "no" else (nos, False)
        mg = allowed + cfg["formula"]["change_step_mg"] if act == "more" else max(0, allowed - cfg["formula"]["change_step_mg"]) if act == "less" else mg
        i = STRENGTHS.index(strength)
        strength = {"stronger": STRENGTHS[min(2, i + 1)], "milder": STRENGTHS[max(0, i - 1)], "strength": ans.get("strength")}.get(act, strength)
    else:
        return conv.say(LINES["cancel"])

    log.add(now, decaf, caf, actual, strength)
    print(f"\n  OUTPUT  decaf {decaf:.1f} mL   caf {caf:.1f} mL   (+ {water:.0f} mL water = {cfg['cup_ml']} mL, {strength}, {actual} mg)\n")
    if uno is None:
        return conv.say(brew_line(SPOKEN[strength], actual, False))
    if uno.use_cup and not uno.cup_present() and not wait_cup(cfg, conv, uno):
        return conv.say(LINES["no_cup"])
    if not uno.brew(caf, decaf, water):
        return conv.say(LINES["no_pumps"])
    conv.say(brew_line(SPOKEN[strength], actual))
    conv.say(LINES[{"removed": "removed", True: "ready", False: "pump_error"}.get(wait_brew(cfg, uno), "pump_error")])


def wait_cup(cfg, conv, uno):
    conv.say(LINES["place_cup"])
    end = time.time() + cfg.get("cup_wait_s", 30)
    while time.time() < end and not uno.cup:
        time.sleep(0.1)
    return bool(uno.cup)


def wait_brew(cfg, uno):
    # Wait for DONE; if the cup is gone for cup_removed_s mid-pour (not just a flicker), send STOP.
    end, missing = time.time() + cfg.get("uno_done_timeout_s", 120), None
    while time.time() < end:
        r = uno.wait_done(0.3)
        if r is not None:
            return r
        if uno.use_cup and uno.cup is False:
            missing = missing or time.time()
            if time.time() - missing > cfg.get("cup_removed_s", 2.0):
                print("[cup] cup removed during pour -> STOP")
                uno.stop(), uno.wait_done(2)
                return "removed"
        else:
            missing = None
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
    except Exception:
        pass
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    uno = None
    if "--no-uno" not in sys.argv:
        try:
            from uno import Uno
            uno = Uno(cfg)
            uno.use_cup = "--no-cup" not in sys.argv and uno.cup_present(1.0) is not None and cfg.get("cup_required", True)
        except Exception as e:
            print(f"[uno] not connected ({e}); values will only be printed")
    from voice import Voice
    audio = Voice(cfg)
    wake = None
    if cfg["audio"].get("wake_word", True):
        try:
            from wake import WakeWord
            a = cfg["audio"]
            wake = WakeWord(os.path.join(HERE, a.get("wake_model", "models/hey_vital.onnx")), os.path.join(HERE, "models"),
                            a.get("wake_threshold", 0.5), a.get("wake_patience", 1))
        except Exception as e:
            print(f"[wake] off ({e})")
    try:
        llm = Gemini(cfg["llm_model"], cfg["llm_timeout_s"])
    except Exception as e:
        print(f"  [Gemini] off ({e}); using scripted wording")
        llm = None
    log = CaffeineLog(os.path.join(HERE, "vital.db"))
    conv = Conversation(audio, llm)
    data = Prefetch(cfg, log, conv)
    start = threading.Event()  # set by Enter or by a cup being placed
    start.reason = ""

    def trigger(reason):
        start.reason = reason
        start.set()

    threading.Thread(target=lambda: [trigger("Enter pressed") for _ in sys.stdin], daemon=True).start()
    if uno and uno.use_cup and cfg.get("cup_starts", True):
        uno.on_cup.append(lambda: threading.Timer(0.6, lambda: uno.cup and trigger("cup placed")).start())  # still there 0.6 s later
    while True:
        print(("\nSay \"Hey Vital\" or press" if wake else "\nPress") + " Enter to start...  (Ctrl+C to quit)")
        if wake:
            audio.wait_for_wake(wake, start)
        else:
            start.clear()
            start.wait()
            print(f"[start] {start.reason}")
        morning(cfg, log, conv, data, uno)
        data.wake.set()  # refresh Google data after each cup


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
