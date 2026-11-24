# The conversation: briefing, questions and answers; Gemini words the briefing and reads odd answers
import json
import os
import re
import threading
import time

from dose import STRENGTHS

HERE = os.path.dirname(os.path.abspath(__file__))
WORDS = {"zero": 0, "none": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
NUM = {**{k: v for k, v in WORDS.items() if v <= 6}, **{str(i): i for i in range(7)}}
LIGHT, MEDIUM, DARK = ("light", "mild", "weak"), ("medium", "regular", "normal"), ("dark", "strong", "bold")
YES = ("yes", "yeah", "yep", "sure", "ok", "okay", "brew")
CONFIRM_HINT = "Say yes, no, more, less, lighter, darker, or cancel."
CHANGE_Q = "What should I change? You can say more, less, lighter, darker, or cancel."
QUESTIONS = {  # kind: (question, hint if not understood)
    "scale": ("How alert are you, on a scale from 1 to 5?", "Just say a number from 1 to 5."),
    "drinks": ("Have you had any caffeine yet today?", "Say no, or how many drinks you've had."),
    "count": ("How many drinks?", "Just say a number."),
    "bedtime": ("What time are you planning to go to bed tonight?", "Say a time, like eleven, or ten thirty."),
    "strength": ("How strong do you want your coffee: light, medium, or dark?", "Say light, medium, or dark.")}
LINES = {
    "cancel": "Okay, I won't brew anything. Press Brew again when you're ready.",
    "place_cup": "Please place your cup under the spout.",
    "ready": "Your coffee is ready. Enjoy.",
    "removed": "The cup was taken away, so I stopped the pumps.",
    "no_cup": "I didn't see a cup, so I won't brew anything. Put your cup in place to start again.",
    "pump_error": "Something went wrong with the pumps. Please check the machine.",
    "no_pumps": "I couldn't reach the pumps. Please check the machine."}
COMMON_LINES = ([q for q, _ in QUESTIONS.values()] + [f"Sorry, I didn't catch that. {h}" for _, h in QUESTIONS.values()]
                + [f"Sorry, I didn't catch that. {CONFIRM_HINT}", CHANGE_Q] + list(LINES.values()))


def rec_question(why, stretched, spoken, actual):
    # One function so pre-recorded and live wording always match.
    extra = (f"I'll cap this cup because {why}. " if why else "") + (
        f"To fit that much caffeine, it will taste a little stronger than {spoken}. " if stretched else "")
    return f"{extra}I recommend a {spoken} coffee with {actual:.0f} milligrams of caffeine. Would you like that brewed?"


def brew_line(spoken, actual, pumping=True):
    return f"Okay: a {spoken} coffee with {actual:.0f} milligrams." + (" Brewing it now." if pumping else "")


def parse_bedtime(text):
    # "eleven", "10:30", "half past ten", "1 am", "midnight" -> (hour 0-23, minute) or None.
    t = re.sub(r"\b([ap])\.? ?m\b\.?", r"\1m", text.lower())
    if "midnight" in t or "noon" in t:
        return (0 if "midnight" in t else 12), 0
    m = re.search(r"(?<!\d)(\d{1,2})(?::(\d{2}))?(?!\d)", t)
    words = re.findall(r"[a-z]+", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
    else:
        nums, i = [], 0
        while i < len(words):
            if words[i] in WORDS:
                n = WORDS[words[i]]
                if n >= 20 and i + 1 < len(words) and WORDS.get(words[i + 1], 99) < 10:  # "forty five"
                    n, i = n + WORDS[words[i + 1]], i + 1
                nums.append(n)
            i += 1
        if not nums:
            return None
        past = "past" in words and ("half" in words and 30 or "quarter" in words and 15)
        h, mi = nums[0], past or (nums[1] if len(nums) > 1 and nums[1] < 60 else 0)
    if h > 23 or mi > 59:
        return None
    if re.search(r"(\d|\b)am\b", t) or "morning" in t:
        h = 0 if h == 12 else h
    elif re.search(r"(\d|\b)pm\b", t) or "night" in t or "evening" in t:
        h = h + 12 if h < 12 else h
    elif h == 12 or 6 <= h <= 11:  # at bedtime "twelve" = midnight, "eleven" = 11 PM
        h = 0 if h == 12 else h + 12
    return h, mi


def quick_parse(text, kind):
    # Understand simple answers on the device; None means "ask Gemini or re-ask".
    words = re.findall(r"[a-z0-9]+", text.lower())
    strength = lambda w: "mild" if w in LIGHT else "regular" if w in MEDIUM else "strong" if w in DARK else None
    if kind in ("drinks", "count") and words:
        num = next((NUM[w] for w in words if w in NUM), None)
        if words[0] in ("no", "nope", "nothing") or num is not None:
            return {"count": num or 0}
        return {"yes": True} if words[0] in ("yes", "yeah", "yep") else None
    for w in words:
        if kind == "scale" and 1 <= NUM.get(w, 0) <= 5:
            return {"feeling": NUM[w]}
        if kind == "strength" and strength(w):
            return {"strength": strength(w)}
        if kind == "confirm":
            for act, ws in (("yes", YES), ("cancel", ("cancel", "stop", "quit")), ("stronger", ("stronger", "darker")),
                            ("milder", ("milder", "weaker", "lighter")), ("more", ("more", "up", "higher")),
                            ("less", ("less", "down", "lower")), ("no", ("no", "nope"))):
                if w in ws:
                    return {"action": act}
            if strength(w):
                return {"action": "strength", "strength": strength(w)}
    if kind == "bedtime" and words:
        bt = parse_bedtime(text)
        return {"bedtime": bt} if bt else None
    return None


def validate(d):
    # Keep only well-formed fields from Gemini's JSON.
    if not isinstance(d, dict):
        return {}
    out = {}
    if isinstance(d.get("feeling"), int) and 1 <= d["feeling"] <= 5:
        out["feeling"] = d["feeling"]
    if isinstance(d.get("count"), int) and 0 <= d["count"] <= 10:
        out["count"] = d["count"]
    if d.get("action") in ("yes", "no", "more", "less", "stronger", "milder", "cancel"):
        out["action"] = d["action"]
    if isinstance(d.get("bedtime"), str) and re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", d["bedtime"]):
        out["bedtime"] = tuple(map(int, d["bedtime"].split(":")))
    if d.get("strength") in STRENGTHS:
        out["strength"] = d["strength"]
        out.setdefault("action", "strength")
    return out


INTERPRET_PROMPT = """You turn a spoken answer into JSON for a coffee machine.
Question asked: "{question}"
Answer heard: "{answer}"
Return ONLY a JSON object with these keys (use null when not stated):
  "feeling": integer 1-5, how alert they are (1 = barely awake, 5 = fully alert),
  "count": integer number of caffeinated drinks already had today,
  "bedtime": planned bedtime tonight as "HH:MM" in 24-hour time (e.g. "23:30"),
  "action": one of "yes","no","more","less","stronger","milder","cancel" (darker = stronger, lighter = milder),
  "strength": one of "mild","regular","strong" (light = mild, medium = regular, dark = strong)."""

BRIEFING_PROMPT = """You are Vital, a friendly coffee machine that speaks to its user in the morning.
Write the spoken briefing: at most 3 short sentences, plain spoken words, no lists, no emoji, no abbreviations.
Start with exactly "{greeting}, {name}." then go straight into their vitals.
Cover, if present: how they slept (say sleep_spoken, not decimals; plus the most notable of quality, deep or
REM sleep, or readiness),
their first event and when it starts, anything important today, and when their day ends if it is a long day.
Use only these facts; do not add numbers, advice or medical claims of your own. Skip facts that are null.
Write "milligrams", never "mg". Do NOT recommend a caffeine amount; that sentence is added after yours.
Do not say you are brewing or making coffee, and do not ask questions; questions come next.
Facts: {facts}"""


class Gemini:
    def __init__(self, model, timeout):
        import requests
        path = os.path.join(HERE, "gemini_key.txt")
        key = os.environ.get("GEMINI_API_KEY") or (open(path).read().strip() if os.path.exists(path) else None)
        if not key:
            raise RuntimeError("no Gemini key: put it in gemini_key.txt")
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self.timeout, self.thinking, self.s = timeout, True, requests.Session()  # one reused connection = faster calls
        self.s.headers["x-goog-api-key"] = key
        threading.Thread(target=lambda: self.s.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", timeout=5), daemon=True).start()

    def _call(self, prompt, as_json, timeout=None):
        cfg = {**({"responseMimeType": "application/json"} if as_json else {}), **({"thinkingConfig": {"thinkingLevel": "minimal"}} if self.thinking else {})}
        r = self.s.post(self.url, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}, timeout=timeout or self.timeout)
        if r.status_code == 400 and self.thinking and "thinking" in r.text.lower():  # model doesn't take that setting
            self.thinking = False
            return self._call(prompt, as_json, timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        c = r.json()["candidates"][0]
        if c.get("finishReason", "STOP") != "STOP":
            raise RuntimeError(f"Gemini stopped early ({c.get('finishReason')})")
        return "".join(p.get("text", "") for p in c["content"]["parts"] if not p.get("thought"))  # replies can be split over parts

    def briefing(self, facts, mg):
        text = self._call(BRIEFING_PROMPT.format(facts=json.dumps(facts, default=str), greeting=greeting(facts["hour"]), name=facts.get("name") or ""), False).strip()
        if len(text) > 600 or re.search(r"\d+\s*(mg|milligram)", text, re.I) or not text.endswith((".", "!", "?")):
            raise ValueError("briefing failed checks")  # too long, cut off, or gave its own dose
        return f"{text} I'd suggest {mg} milligrams of caffeine."

    def interpret(self, question, answer):
        return validate(json.loads(self._call(INTERPRET_PROMPT.format(question=question, answer=answer), True, min(4, self.timeout))))


def greeting(hour):
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


def scripted_briefing(f, mg):
    # Used when Gemini is off or fails.
    parts = [f"{greeting(f['hour'])}, {f['name']}." if f.get("name") else f"{greeting(f['hour'])}."]
    if f.get("sleep_spoken"):
        parts.append(f"You slept {f['sleep_spoken']}" + (f", with {f['deep_sleep_minutes']} minutes of deep sleep." if f.get("deep_sleep_minutes") is not None else "."))
    if f.get("readiness_out_of_100") is not None:
        parts.append(f"Your readiness is {f['readiness_out_of_100']} out of 100.")
    if f.get("illness_signs"):
        parts.append("A few of your readings look like you might be coming down with something.")
    if f.get("first_event"):
        parts.append(f"Your first event is {f['first_event']}.")
    others = [e for e in f.get("important_today") or [] if not (f.get("first_event") or "").startswith(e.split(" at ")[0])]
    if others:
        parts.append("Important today: " + ", and ".join(others) + ".")
    if f.get("day_length_hours"):
        parts.append(f"It's a long one: about {round(f['day_length_hours'])} hours, ending at {f['day_ends']}.")
    elif f.get("day_ends"):
        parts.append(f"Your day wraps up at {f['day_ends']}.")
    parts += [p[0].upper() + p[1:] + "." for p in f.get("problems", [])]
    return " ".join(parts + [f"I'd suggest {mg} milligrams of caffeine."])


def speakable(text):
    for pat, rep in ((r"(\d)\s*mg\b", r"\1 milligrams"), (r"\bmg\b", "milligrams"), (r"(\d)\s*%", r"\1 percent"), (r"(\d)\s*bpm\b", r"\1 beats per minute")):
        text = re.sub(pat, rep, text)
    return text


class Conversation:
    def __init__(self, audio, llm):
        self.audio, self.llm = audio, llm
        audio.prepare(COMMON_LINES)

    def say(self, text):
        self.audio.say(speakable(text))

    def briefing_text(self, facts, mg):
        if self.llm:
            try:
                t = time.time()
                text = self.llm.briefing(facts, mg)
                print(f"  [time] Gemini briefing {time.time() - t:.1f} s")
                return speakable(text)
            except Exception as e:
                print(f"  [Gemini] briefing failed ({e}), using the script")
        return speakable(scripted_briefing(facts, mg))

    def prerender(self, text, background=False):
        if background:
            return threading.Thread(target=self.audio.prerender, args=(speakable(text),), daemon=True).start()
        return self.audio.prerender(speakable(text))

    def ask(self, kind, question=None, hint=CONFIRM_HINT):
        # Ask with one retry; returns a dict, or {} if no usable answer.
        question, hint = QUESTIONS[kind] if question is None else (question, hint)
        q = question
        for _ in range(2):
            self.audio.prepare_listen(speakable(q), kind)  # mic + listener open while the question plays
            self.say(q)
            heard = self.audio.listen(kind, speakable(q))
            got = quick_parse(heard, kind) if heard else None
            if not got and heard and self.llm and len(heard.split()) > 1:  # one stray word: just re-ask
                try:
                    t = time.time()
                    got = self.llm.interpret(question, heard)
                    print(f"  [Gemini] understood {got} ({time.time() - t:.1f} s)")
                except Exception as e:
                    print(f"  [Gemini] failed ({e})")
            if got:
                return got
            q = f"Sorry, I didn't catch that. {hint}"
        return {}
