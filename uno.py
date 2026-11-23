# Arduino Uno over USB: "p1,p2,p3\n" -> OK / DONE / ERR,<why> / STOPPED; SENSOR,1/0 reports the cup.
import glob
import queue
import threading
import time


class Uno:
    def __init__(self, cfg):
        import serial
        from serial.tools import list_ports
        self.cfg, self.lines, self.cup, self.on_cup, self.use_cup = cfg, queue.Queue(), None, [], False
        port = cfg.get("uno_port") or next(iter(sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))), None) or next(
            (p.device for p in list_ports.comports() if p.vid == 0x2341 or "arduino" in (p.description or "").lower()), None)
        if not port:
            raise RuntimeError("no Uno found: is the USB cable plugged in?")
        self.ser, self.heard = serial.Serial(port, cfg.get("uno_baud", 9600), timeout=0.2), threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()
        print(f"[uno] connected on {port}, waiting for it to boot...")
        self._until(("READY",), 6)  # opening the port reboots the Uno
        while not self.lines.empty():
            self.lines.get_nowait()

    def _reader(self):
        # SENSOR lines update self.cup (and fire on_cup when a cup arrives); everything else is queued.
        while True:
            try:
                raw = self.ser.readline().decode(errors="replace").strip()
            except Exception:
                time.sleep(0.5)
                continue
            if raw.upper().startswith("SENSOR,"):
                now = raw.split(",", 1)[1].strip() == "1"
                was, self.cup = self.cup, now
                if now != was:
                    print(f"  [uno] {'cup in place' if now else 'no cup'}")
                self.heard.set()
                if now and was is False:
                    for cb in self.on_cup:
                        cb()
            elif raw:
                print(f"  [uno] <- {raw}")
                self.lines.put(raw)

    def _send(self, text):
        self.ser.write((text + "\n").encode())

    def _until(self, kinds, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.lines.get(timeout=max(0, end - time.time()))
            except queue.Empty:
                return None
            if line.split(",")[0].upper() in kinds:
                return line.upper()
        return None

    def cup_present(self, timeout=0.6):
        self.heard.clear()
        self._send("SENSOR?")
        self.heard.wait(timeout)
        return self.cup

    def brew(self, caf, decaf, water):
        # Every pump gets +uno_prime_ml: that much stays in the tubing before anything reaches the cup.
        vols, prime = {"caf": caf, "decaf": decaf, "water": water}, float(self.cfg.get("uno_prime_ml", 6))
        msg = ",".join(f"{vols[p] + prime:.1f}" if p in vols else "0.0" for p in self.cfg.get("uno_pump_order", ["caf", "decaf", "water"]))
        print(f"  [uno] -> {msg}  (+{prime:g} mL per pump for the tubing)")
        for _ in range(2):
            self._send(msg)
            got = self._until(("OK", "ERR"), self.cfg.get("uno_ack_timeout_s", 3))
            if got != "ERR,MESSAGE":  # only a garbled message is safe to resend
                return got == "OK"
        return False

    def wait_done(self, timeout):
        got = self._until(("DONE", "ERR", "STOPPED"), timeout)
        return None if got is None else got == "DONE"

    def stop(self):
        self._send("STOP")
