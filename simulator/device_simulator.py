"""KapD device fleet simulator.

Generates realistic HL7 v2.4 ORU^R01 messages from Sigma immunoassay,
chemistry and haematology analysers across three NSW hospital sites,
including duplicate patients, pre analytical errors and critical values.

Run against a live server:
    python -m simulator.device_simulator --url http://localhost:8000 --count 200 --rate 20
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")

FAMILY = [
    "Nguyen",
    "Smith",
    "Williams",
    "Chen",
    "Patel",
    "Brown",
    "Singh",
    "Wilson",
    "Kaur",
    "Taylor",
    "Ahmed",
    "Martin",
    "Anderson",
    "Tran",
    "Thompson",
    "Rahman",
    "White",
    "Kelly",
    "Li",
    "Hughes",
]
GIVEN_F = ["Olivia", "Charlotte", "Mia", "Aisha", "Priya", "Grace", "Lan", "Zara", "Emily", "Fatima"]
GIVEN_M = ["Jack", "Noah", "William", "Arjun", "Minh", "Oliver", "Yusuf", "Lachlan", "Wei", "Thomas"]
POSTCODES = ["2065", "2067", "2145", "2150", "2050", "2000", "2112", "2077", "2031", "2170"]
SITES = ["RNSH", "RPA", "WMD"]


@dataclass(frozen=True)
class Test:
    loinc: str
    name: str
    units: str
    low: float
    high: float
    tube: str
    device_type: str


TESTS = [
    Test("89579-7", "Troponin I (high sensitivity)", "ng/L", 0, 26, "LIH", "sigma"),
    Test("3016-3", "TSH", "mIU/L", 0.4, 4.0, "SST", "sigma"),
    Test("2276-4", "Ferritin", "ug/L", 30, 500, "SST", "sigma"),
    Test("2823-3", "Potassium", "mmol/L", 3.5, 5.2, "LIH", "chem"),
    Test("2951-2", "Sodium", "mmol/L", 135, 145, "LIH", "chem"),
    Test("2345-7", "Glucose", "mmol/L", 3.0, 7.7, "LIH", "chem"),
    Test("2160-0", "Creatinine", "umol/L", 45, 110, "LIH", "chem"),
    Test("718-7", "Haemoglobin", "g/L", 115, 165, "EDTA", "haem"),
]

DEVICES = {
    "sigma": [
        ("SIG-001", "RNSH"),
        ("SIG-002", "RNSH"),
        ("SIG-003", "RPA"),
        ("SIG-004", "RPA"),
        ("SIG-005", "WMD"),
        ("SIG-006", "WMD"),
    ],
    "chem": [("KCH-101", "RNSH"), ("KCH-102", "RPA"), ("KCH-103", "WMD")],
    "haem": [("KHM-201", "RNSH"), ("KHM-202", "WMD")],
}
MODELS = {"sigma": "KapD Sigma Immunoassay", "chem": "KapD Chem 800", "haem": "KapD Haem 5"}
FIRMWARE = {"sigma": "4.2.1", "chem": "2.8.0", "haem": "1.9.3"}
LOTS = {"sigma": ["LOT-S24A117", "LOT-S24A118"], "chem": ["LOT-C24B031"], "haem": ["LOT-H24C009"]}


def medicare_number(rng: random.Random) -> str:
    """Generate a Medicare number with a valid check digit (synthetic, not a real person)."""
    digits = [rng.randint(2, 6)] + [rng.randint(0, 9) for _ in range(7)]
    check = sum(d * w for d, w in zip(digits, [1, 3, 7, 9, 1, 3, 7, 9], strict=True)) % 10
    return "".join(map(str, digits)) + str(check) + str(rng.randint(0, 9)) + "1"


class DeviceSimulator:
    def __init__(self, seed: int | None = 42, patients: int = 60):
        self.rng = random.Random(seed)
        self.counter = 0
        self.patients = [self._patient() for _ in range(patients)]

    def _patient(self) -> dict:
        sex = self.rng.choice("MF")
        dob = datetime(1935, 1, 1) + timedelta(days=self.rng.randint(0, 32000))
        return {
            "family": self.rng.choice(FAMILY),
            "given": self.rng.choice(GIVEN_M if sex == "M" else GIVEN_F),
            "dob": dob.strftime("%Y%m%d"),
            "sex": sex,
            "medicare": medicare_number(self.rng),
            "postcode": self.rng.choice(POSTCODES),
            "mrns": {site: f"{site[:2]}{self.rng.randint(100000, 999999)}" for site in SITES},
            "home": self.rng.choice(SITES),
        }

    def _variant(self, p: dict) -> dict:
        """A record for the same person that differs the way real registrations do."""
        v = {**p, "mrns": {s: f"{s[:2]}{self.rng.randint(100000, 999999)}" for s in SITES}}
        change = self.rng.choice(["typo", "dob_swap", "no_medicare"])
        if change == "typo" and len(p["family"]) > 3:
            i = self.rng.randint(1, len(p["family"]) - 2)
            name = list(p["family"])
            name[i], name[i + 1] = name[i + 1], name[i]
            v["family"] = "".join(name)
        elif change == "dob_swap" and int(p["dob"][6:8]) <= 12:
            v["dob"] = p["dob"][:4] + p["dob"][6:8] + p["dob"][4:6]
        else:
            v["medicare"] = ""
        return v

    def _value(self, test: Test, critical: bool) -> float:
        span = test.high - test.low
        if critical:
            if test.loinc == "89579-7":
                return round(self.rng.uniform(150, 3000), 0)
            if test.loinc == "718-7":
                return round(self.rng.uniform(45, 68), 0)
            if test.loinc == "2823-3":
                return round(self.rng.choice([self.rng.uniform(6.4, 7.5), self.rng.uniform(2.0, 2.4)]), 1)
        value = self.rng.gauss(test.low + span / 2, span / 3.2)
        if test.loinc == "89579-7":
            value = abs(self.rng.lognormvariate(1.8, 0.8))
        return round(max(value, 0.01), 2 if span < 10 else 1)

    @staticmethod
    def _flag(test: Test, value: float) -> str:
        if value > test.high * 1.3:
            return "HH"
        if value > test.high:
            return "H"
        if value < test.low * 0.7:
            return "LL"
        if value < test.low:
            return "L"
        return "N"

    def message(self, batch: str | None = None, issue: str | None = None, critical: bool | None = None) -> str:
        rng = self.rng
        self.counter += 1
        test = rng.choice(TESTS)
        patient = rng.choice(self.patients)
        fleet = DEVICES[test.device_type]
        local = [d for d in fleet if d[1] == patient["home"]]
        # Most patients attend their home hospital, some present elsewhere.
        serial, site = rng.choice(local if local and rng.random() < 0.85 else fleet)
        if rng.random() < 0.08:
            patient = self._variant(patient)
        issue = (
            issue
            if issue is not None
            else rng.choices(
                ["none", "haemolysed", "clotted", "insufficient", "wrong_tube"], [0.88, 0.05, 0.03, 0.02, 0.02]
            )[0]
        )
        critical = critical if critical is not None else rng.random() < 0.10
        value = self._value(test, critical)

        h, vol, clot, tube = rng.uniform(2, 30), rng.uniform(300, 1400), rng.uniform(0, 0.25), test.tube
        minutes = rng.uniform(15, 150)
        if issue == "haemolysed":
            h = rng.uniform(80, 400)
            minutes = rng.uniform(180, 420)
            if test.loinc == "2823-3":
                value = round(value + rng.uniform(1.2, 2.2), 1)  # haemolysis falsely raises potassium
        elif issue == "clotted":
            clot = rng.uniform(0.6, 0.95)
        elif issue == "insufficient":
            vol = rng.uniform(20, 120)
        elif issue == "wrong_tube":
            tube = rng.choice([t for t in ("SST", "EDTA", "LIH", "CIT") if t != test.tube])

        now = datetime.now(SYDNEY)
        collected = now - timedelta(minutes=minutes)
        resulted = now - timedelta(seconds=rng.randint(5, 90))
        ts = "%Y%m%d%H%M%S%z"
        control_id = f"{serial}{now.strftime('%H%M%S')}{self.counter:06d}{rng.randint(100, 999)}"
        order_id = f"ORD{rng.randint(1000000, 9999999)}"
        lot = batch or rng.choice(LOTS[test.device_type])
        operator = f"OP{rng.randint(10, 60)}"
        ids = f"{patient['mrns'][site]}^^^{site}^MR"
        if patient["medicare"]:
            ids += f"~{patient['medicare']}^^^AUSHIC^MC"

        segments = [
            f"MSH|^~\\&|{MODELS[test.device_type].replace(' ', '_').upper()}|{site}|KAPCONNECT|{site}|"
            f"{now.strftime(ts)}||ORU^R01|{control_id}|P|2.4|||AL|NE|AUS",
            f"PID|1||{ids}||{patient['family'].upper()}^{patient['given']}||{patient['dob']}|{patient['sex']}"
            f"|||1 Example St^^Sydney^NSW^{patient['postcode']}^AUS",
            f"PV1|1|{rng.choice(['E', 'I', 'O'])}|{rng.choice(['ED', '4B', 'ICU', 'CCU', 'OPD'])}^^^{site}",
            f"ORC|RE|{order_id}",
            f"OBR|1|{order_id}||{test.loinc}^{test.name}^LN|||{collected.strftime(ts)}|||||||||"
            f"DR{rng.randint(100, 999)}^CLINICIAN^A|||||||{resulted.strftime(ts)}|||F",
            f"OBX|1|NM|{test.loinc}^{test.name}^LN||{value}|{test.units}|{test.low:g}-{test.high:g}|"
            f"{self._flag(test, value)}|||F|||{resulted.strftime(ts)}",
            f"ZRP|{serial}|{FIRMWARE[test.device_type]}|{operator}|{lot}|{site}|{MODELS[test.device_type]}",
            f"ZSQ|{h:.1f}|{rng.uniform(5, 80):.1f}|{rng.uniform(0, 8):.1f}|{vol:.0f}|{tube}|{test.tube}|"
            f"{clot:.2f}|{minutes:.0f}",
        ]
        return "\r".join(segments)


def main() -> None:
    import httpx

    parser = argparse.ArgumentParser(description="Stream simulated KapD device results into KapConnect")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--rate", type=float, default=10.0, help="messages per second")
    parser.add_argument("--batch", default=None, help="force a reagent batch, useful for recall drills")
    parser.add_argument("--username", default="sigma.device")
    parser.add_argument("--password", default="kapconnect")
    args = parser.parse_args()

    sim = DeviceSimulator(seed=None)
    with httpx.Client(base_url=args.url, timeout=10) as client:
        token = client.post("/api/v1/auth/token", data={"username": args.username, "password": args.password}).json()[
            "access_token"
        ]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/hl7-v2"}
        stats = {"accepted": 0, "held": 0, "rejected": 0, "failover": 0}
        for i in range(args.count):
            resp = client.post("/api/v1/ingest/hl7", content=sim.message(batch=args.batch), headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                stats["accepted"] += 1
                stats["held"] += int(body["screening"]["hold"])
                stats["failover"] += sum(int(r["failover"]) for r in body["results"])
            else:
                stats["rejected"] += 1
            if (i + 1) % 25 == 0:
                print(f"sent {i + 1}/{args.count} {stats}")
            time.sleep(1 / args.rate)
        print(f"done {stats}")


if __name__ == "__main__":
    main()
