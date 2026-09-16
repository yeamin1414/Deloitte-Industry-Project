"""AI pre analytical screening.

A RandomForest classifier flags the four most common pre
analytical errors before a result is released: haemolysis, clotting,
insufficient volume and wrong tube type. Deterministic guardrails run first,
so a model can never release a sample that a hard rule rejects.

The model is trained on synthetic data generated from published interference
index ranges. Metrics reported by /api/v1/screening/model are on a synthetic
holdout set and must be revalidated on real laboratory data before clinical use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import train_test_split

log = logging.getLogger(__name__)

LABELS = ["ok", "haemolysed", "clotted", "insufficient_volume", "wrong_tube"]
FEATURES = [
    "haemolysis_index",
    "lipaemia_index",
    "icterus_index",
    "sample_volume_ul",
    "tube_mismatch",
    "clot_signal",
    "minutes_since_collection",
]
MIN_VOLUME_UL = 150.0
HOLD_CONFIDENCE = 0.55


@dataclass
class ScreeningResult:
    label: str
    confidence: float
    hold: bool
    reasons: list[str]
    source: str  # model, guardrail or not_screened

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def synthesize(n: int = 8000, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    probs = [0.78, 0.08, 0.05, 0.045, 0.045]
    y = rng.choice(len(LABELS), size=n, p=probs)
    X = np.zeros((n, len(FEATURES)))
    for i, label in enumerate(y):
        h = rng.lognormal(2.6, 0.6)  # typically below 40
        lip = rng.gamma(2.0, 20.0)
        ict = rng.gamma(1.5, 2.0)
        vol = rng.uniform(250, 1500)
        mismatch = float(rng.random() < 0.01)
        clot = rng.beta(1.5, 8)
        minutes = rng.uniform(5, 240)
        if label == 1:  # haemolysed
            h = rng.uniform(45, 600)
            minutes = rng.uniform(60, 600)
        elif label == 2:  # clotted
            clot = rng.uniform(0.45, 1.0)
            vol = rng.uniform(150, 1200)
        elif label == 3:  # insufficient volume
            vol = rng.uniform(10, 190)
        elif label == 4:  # wrong tube
            mismatch = 1.0
        X[i] = [h, lip, ict, vol, mismatch, clot, minutes]
    return X, y


class PreAnalyticalScreen:
    def __init__(self, seed: int = 7):
        X, y = synthesize(seed=seed)
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=seed, stratify=y)
        self.model = RandomForestClassifier(
            n_estimators=150, max_depth=12, class_weight="balanced", random_state=seed, n_jobs=1
        )
        self.model.fit(X_train, y_train)
        pred = self.model.predict(X_test)
        errors_true = y_test != 0
        errors_caught = (pred != 0) & errors_true
        self.metrics = {
            "training_rows": int(len(X_train)),
            "holdout_rows": int(len(X_test)),
            "macro_f1": round(float(f1_score(y_test, pred, average="macro")), 4),
            "error_detection_recall": round(float(errors_caught.sum() / max(errors_true.sum(), 1)), 4),
            "false_hold_rate": round(float(((pred != 0) & ~errors_true).sum() / max((~errors_true).sum(), 1)), 4),
            "per_class": {
                LABELS[int(k)]: {m: round(v, 4) for m, v in vals.items()}
                for k, vals in classification_report(y_test, pred, output_dict=True).items()
                if k.isdigit()
            },
            "feature_importance": {
                f: round(float(w), 4)
                for f, w in sorted(zip(FEATURES, self.model.feature_importances_, strict=True), key=lambda t: -t[1])
            },
            "data": "synthetic, generated from interference index ranges; revalidate on site data",
        }
        log.info("screening model ready: macro F1 %.3f", self.metrics["macro_f1"])

    @staticmethod
    def _vector(q: dict) -> list[float]:
        mismatch = float(bool(q.get("expected_tube_code")) and q.get("tube_code") != q.get("expected_tube_code"))
        return [
            float(q.get("haemolysis_index", 0)),
            float(q.get("lipaemia_index", 0)),
            float(q.get("icterus_index", 0)),
            float(q.get("sample_volume_ul", 0)),
            mismatch,
            float(q.get("clot_signal", 0)),
            float(q.get("minutes_since_collection", 0)),
        ]

    @staticmethod
    def _reasons(q: dict, vec: list[float]) -> list[str]:
        reasons = []
        if vec[0] >= 45:
            reasons.append(f"Haemolysis index {vec[0]:.0f} is above 45")
        if vec[3] < MIN_VOLUME_UL:
            reasons.append(f"Sample volume {vec[3]:.0f} uL is below {MIN_VOLUME_UL:.0f} uL")
        if vec[4]:
            reasons.append(f"Tube {q.get('tube_code')} received, {q.get('expected_tube_code')} expected")
        if vec[5] >= 0.45:
            reasons.append(f"Aspiration clot signal {vec[5]:.2f} is above 0.45")
        if vec[6] > 360:
            reasons.append(f"{vec[6]:.0f} minutes since collection exceeds stability window")
        return reasons

    def screen(self, quality: dict | None) -> ScreeningResult:
        if not quality:
            return ScreeningResult("not_screened", 0.0, False, ["No sample quality data from device"], "not_screened")
        vec = self._vector(quality)
        reasons = self._reasons(quality, vec)

        # Guardrails: deterministic rules always win.
        if vec[4]:
            return ScreeningResult("wrong_tube", 1.0, True, reasons, "guardrail")
        if vec[3] < 50:
            return ScreeningResult("insufficient_volume", 1.0, True, reasons, "guardrail")

        proba = self.model.predict_proba([vec])[0]
        idx = int(np.argmax(proba))
        label, confidence = LABELS[idx], float(proba[idx])
        hold = label != "ok" and confidence >= HOLD_CONFIDENCE
        if label != "ok" and not hold:
            reasons.append("Low confidence flag, released with a quality comment")
        return ScreeningResult(label, round(confidence, 4), hold, reasons, "model")
