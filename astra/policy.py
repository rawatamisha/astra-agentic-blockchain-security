"""Admission policy applied to measured trust and specialist findings."""

TRUST_THRESHOLD = .70
SPECIALIST_REVIEW_THRESHOLD = .75
PEAK_RISK_WEIGHT = .50
STRONG_INDICATOR_RISK_FLOOR = .76


def apply_admission_policy(trust, *, spend_conflict=False):
    factor = trust["factor"]
    risks = trust.get("risks", {})
    weighted_risk = trust.get("attack_risk")
    positive_trust = trust.get("positive_trust")
    original_factor = trust.get("original_factor", factor)
    adjusted_risk = None
    if (factor is not None and weighted_risk is not None and positive_trust is not None
            and risks and all(score is not None for score in risks.values())):
        original_factor = .60 * positive_trust + .40 * (1 - weighted_risk)
        peak_risk = max(risks.values())
        if spend_conflict or peak_risk >= SPECIALIST_REVIEW_THRESHOLD:
            adjusted_risk = max(peak_risk, STRONG_INDICATOR_RISK_FLOOR)
        else:
            adjusted_risk = (1 - PEAK_RISK_WEIGHT) * weighted_risk + PEAK_RISK_WEIGHT * peak_risk
        factor = .60 * positive_trust + .40 * (1 - adjusted_risk)
    escalations = [
        {"dimension": name,
         "reason": f"Specialist risk {score:.2f} meets the {SPECIALIST_REVIEW_THRESHOLD:.2f} review threshold."}
        for name, score in risks.items()
        if score is not None and score >= SPECIALIST_REVIEW_THRESHOLD
        and not (name == "finney" and spend_conflict)
    ]
    if spend_conflict:
        escalations.append({"dimension": "finney",
                            "reason": "Observed spend conflict. Private-block evidence is unavailable."})
    if factor is None:
        formula = "MORE_EVIDENCE"
    elif factor < TRUST_THRESHOLD:
        formula = "REJECT"
    else:
        formula = "ACCEPT"
    recommendation = "REVIEW_REQUIRED" if formula == "ACCEPT" and escalations else formula
    return {**trust, "original_factor": original_factor, "adjusted_attack_risk": adjusted_risk,
            "factor": factor, "formula_recommendation": formula, "recommendation": recommendation,
            "escalations": escalations}
