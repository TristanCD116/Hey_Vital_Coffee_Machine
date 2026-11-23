# Today's Google Health (Fitbit) data and Google Calendar events; separate OAuth tokens for each.
import datetime as dt
import glob
import os
from concurrent.futures import ThreadPoolExecutor
from statistics import mean

HERE = os.path.dirname(os.path.abspath(__file__))
HEALTH_API = "https://health.googleapis.com/v4/users/me/dataTypes"
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
HEALTH_SCOPES = [f"https://www.googleapis.com/auth/googlehealth.{s}.readonly"
                 for s in ("activity_and_fitness", "health_metrics_and_measurements", "sleep")]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


def _session(scopes, token_name):
    # Saved token, refreshed if needed; first run opens a browser to sign in.
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    token = os.path.join(HERE, token_name)
    creds = Credentials.from_authorized_user_file(token, scopes) if os.path.exists(token) else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            secrets = sorted(glob.glob(os.path.join(HERE, "client_secret*.json")), key=os.path.getmtime)
            if not secrets:
                raise FileNotFoundError("put client_secret*.json in the vital folder")
            creds = InstalledAppFlow.from_client_secrets_file(secrets[-1], scopes).run_local_server(port=0)
        with open(token, "w") as f:
            f.write(creds.to_json())
    return AuthorizedSession(creds)


def _points(s, data_type, filt=None):
    url, out, page = f"{HEALTH_API}/{data_type}/dataPoints", [], None
    for _ in range(20):
        params = {"pageSize": 1000, **({"filter": filt} if filt else {}), **({"pageToken": page} if page else {})}
        r = s.get(url, params=params, timeout=20)
        if r.status_code == 400 and filt:  # filter rejected: fetch everything instead
            return _points(s, data_type)
        if r.status_code != 200:
            raise RuntimeError(f"{data_type}: HTTP {r.status_code} {r.text[:300]}")
        out += r.json().get("dataPoints", [])
        page = r.json().get("nextPageToken")
        if not page:
            break
    return out


def _date(d):
    try:
        return dt.date(int(d["year"]), int(d["month"]), int(d["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _daily(s, data_type, field, key, since, today):
    snake = data_type.replace("-", "_")
    out = {}
    for p in _points(s, data_type, f'{snake}.date >= "{since}" AND {snake}.date <= "{today}"'):
        b = p.get(field) or {}
        d = _date(b.get("date", {}))
        if d and since <= d <= today and b.get(key) is not None:
            out[d] = float(b[key])
    return out


def _last_night(series, today):
    # (last night's value, average of the days before), or (None, None).
    if not series or (today - max(series)).days > 1:
        return None, None
    day = max(series)
    prior = [v for d, v in series.items() if d < day]
    return series[day], (mean(prior) if prior else None)


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _sleep(points, cutoff):
    # Last night's main sleep: hours, efficiency, deep and REM minutes.
    nights = []
    for p in points:
        sl = p.get("sleep") or {}
        sm, end = sl.get("summary") or {}, sl.get("interval", {}).get("endTime")
        if end and _ts(end) >= cutoff and sm.get("minutesAsleep") is not None:
            nights.append((bool((sl.get("metadata") or {}).get("mainSleep")), float(sm["minutesAsleep"]), sl, sm))
    if not nights:
        return {}
    _, asleep, sl, sm = max(nights, key=lambda n: n[:2])
    in_bed = float(sm.get("minutesInSleepPeriod") or asleep + float(sm.get("minutesAwake") or 0))
    stages = {st.get("type"): float(st["minutes"]) for st in sm.get("stagesSummary") or [] if st.get("minutes") is not None}
    if not stages:
        for st in sl.get("stages") or []:
            stages[st.get("type")] = stages.get(st.get("type"), 0) + (_ts(st["endTime"]) - _ts(st["startTime"])).total_seconds() / 60
    return {"sleep_hours": round(asleep / 60, 2), "sleep_efficiency_pct": round(100 * asleep / in_bed) if in_bed else None,
            "deep_min": round(stages["DEEP"]) if "DEEP" in stages else None, "rem_min": round(stages["REM"]) if "REM" in stages else None}


def get_health():
    # Returns (inputs for the formula, error or None).
    try:
        s = _session(HEALTH_SCOPES, "token_health.json")
        today = dt.date.today()
        since, cutoff = today - dt.timedelta(days=14), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=18)
        daily = lambda t, f, k: lambda: _daily(s, t, f, k, since, today)
        jobs = {"rhr": daily("daily-resting-heart-rate", "dailyRestingHeartRate", "beatsPerMinute"),
                "hrv": daily("daily-heart-rate-variability", "dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds"),
                "spo2": daily("daily-oxygen-saturation", "dailyOxygenSaturation", "averagePercentage"),
                "br": daily("daily-respiratory-rate", "dailyRespiratoryRate", "breathsPerMinute"),
                "temp": lambda: _points(s, "daily-sleep-temperature-derivations", f'daily_sleep_temperature_derivations.date >= "{today - dt.timedelta(days=1)}"'),
                "sleep": lambda: _points(s, "sleep", f'sleep.interval.end_time >= "{cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")}"')}
        got = {}
        with ThreadPoolExecutor(len(jobs)) as ex:  # in parallel: much faster
            for k, fut in {k: ex.submit(f) for k, f in jobs.items()}.items():
                try:
                    got[k] = fut.result()
                except Exception as e:  # one failed type doesn't sink the rest
                    print(f"  [health] {k}: {e}")
                    got[k] = [] if k in ("temp", "sleep") else {}
        temp = None
        for p in got["temp"]:
            b = p.get("dailySleepTemperatureDerivations") or {}
            d = _date(b.get("date", {}))
            if d and (today - d).days <= 1 and None not in (b.get("nightlyTemperatureCelsius"), b.get("baselineTemperatureCelsius")):
                temp = float(b["nightlyTemperatureCelsius"]) - float(b["baselineTemperatureCelsius"])
        (rhr, rhr_b), (hrv, hrv_b), (br, br_b) = (_last_night(got[k], today) for k in ("rhr", "hrv", "br"))
        r = lambda x: None if x is None else round(x, 2)
        out = {"rhr_delta": r(rhr - rhr_b) if None not in (rhr, rhr_b) else None,
               "hrv_delta_pct": r(100 * (hrv - hrv_b) / hrv_b) if hrv is not None and hrv_b else None,
               "skin_temp_dev": r(temp), "spo2_avg": r(_last_night(got["spo2"], today)[0]),
               "breath_rate_delta": r(br - br_b) if None not in (br, br_b) else None,
               "sleep_hours": None, "sleep_efficiency_pct": None, "deep_min": None, "rem_min": None}
        out.update(_sleep(got["sleep"], cutoff))
        return out, None
    except Exception as e:
        print(f"  [health error] {e}")
        return {}, "I couldn't reach Google Health"


def _local(s):
    t = _ts(s)
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def get_events(now):
    # Returns (rest of today's events, error); flagged = coloured red (Tomato) in Google Calendar.
    try:
        s = _session(CALENDAR_SCOPES, "token_calendar.json")
        midnight = now.replace(hour=0, minute=0, second=0)
        r = s.get(CALENDAR_API, timeout=10, params={
            "timeMin": midnight.astimezone().isoformat(), "timeMax": now.replace(hour=23, minute=59, second=59).astimezone().isoformat(),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 50})
        r.raise_for_status()
        events = []
        for e in r.json().get("items", []):
            st, en, title = e.get("start", {}), e.get("end", {}), e.get("summary", "(no title)")
            ev = {"title": title, "flagged": e.get("colorId") == "11"}
            if "dateTime" in st and _local(en["dateTime"]) > now:
                events.append({**ev, "start": _local(st["dateTime"]), "end": _local(en["dateTime"]), "all_day": False})
            elif "date" in st:
                events.append({**ev, "start": midnight, "end": midnight.replace(hour=23, minute=59), "all_day": True})
        return events, None
    except Exception as e:
        print(f"  [calendar error] {e}")
        return [], "I couldn't reach your calendar"
