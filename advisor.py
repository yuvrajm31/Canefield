"""Rule-based recommendation engine for Canefield.

Turns a saved farm profile into concrete, quantified actions (irrigation,
fertilizer, alerts). Pure Python so it can be unit-tested without a database.

All agronomic constants are screening guidelines, not certified advice.
"""
from math import ceil

ACRE_M2 = 4046.86
PUMP_KL_PER_H = 36.0          # assumed pump delivery for the whole farm
SESSION_MAX_H = 12            # longest sensible single pumping session

# Target root-zone soil moisture (%) and effective root depth (mm) per stage
STAGE_TARGET = {'Early': 24.0, 'Mid': 28.0, 'Late': 20.0}
STAGE_ROOT_MM = {'Early': 300.0, 'Mid': 600.0, 'Late': 750.0}
# Share of applied water that reaches the root zone
METHOD_EFF = {'Drip': 0.90, 'Sprinkler': 0.75, 'Furrow': 0.60, 'Flood': 0.50}

# (product, kg per acre, why)
FERT_PLAN = {
    'Early': [('DAP', 50, 'Phosphorus drives root and tiller establishment in the first months after planting.')],
    'Mid': [
        ('Urea', 55, 'The grand-growth phase has the highest nitrogen demand; a split top-dress keeps cane elongating.'),
        ('MOP', 30, 'Potassium improves water-use efficiency and sucrose accumulation during grand growth.'),
    ],
    'Late': [('MOP', 20, 'A light potash dose in the ripening phase supports sugar build-up and stalk strength.')],
}

SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}


def _num(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def moisture_target(stage):
    return STAGE_TARGET.get(stage or 'Mid', 28.0)


def moisture_status(moisture, stage):
    """Return ('low'|'ok'|'wet'|'unknown', target, deficit_pct)."""
    target = moisture_target(stage)
    m = _num(moisture)
    if m is None:
        return 'unknown', target, 0.0
    deficit = round((target - m) / target * 100, 1)
    if deficit > 10:
        return 'low', target, deficit
    if m > target + 10:
        return 'wet', target, deficit
    return 'ok', target, deficit


def irrigation_plan(farm):
    stage = farm.get('growth_stage') or 'Mid'
    method = farm.get('irrigation_method') or 'Drip'
    area = _num(farm.get('area_acres')) or 0
    m = _num(farm.get('soil_moisture'))
    target = moisture_target(stage)
    if m is None or area <= 0 or m >= target:
        return None
    depth_mm = (target - m) / 100 * STAGE_ROOT_MM.get(stage, 600) / METHOD_EFF.get(method, 0.75)
    kl = depth_mm * area * ACRE_M2 / 1000
    hours = kl / PUMP_KL_PER_H
    return {
        'kl': round(kl), 'hours': round(hours, 1), 'depth_mm': round(depth_mm, 1),
        'sessions': max(1, ceil(hours / SESSION_MAX_H)), 'target': target,
        'deficit': round((target - m) / target * 100, 1), 'moisture': m,
        'method': method, 'stage': stage,
    }


def build_for_farm(farm, last_advisory=None, recent_done=()):
    """Return a list of recommendation dicts for one farm.

    recent_done: iterable of (kind, product) already completed recently, used
    to avoid re-recommending the same job.
    """
    recs = []
    name = farm.get('farm_name') or 'Farm'
    stage = farm.get('growth_stage') or 'Mid'
    area = _num(farm.get('area_acres')) or 0
    soil = farm.get('soil_type') or 'soil'
    m = _num(farm.get('soil_moisture'))
    ph = _num(farm.get('soil_ph'))
    done = set(recent_done)

    # ---- Irrigation / waterlogging / missing data ------------------------
    if m is None:
        recs.append(dict(kind='alert', severity='low', product=None,
            title=f'Add a soil-moisture reading for {name}',
            reason='Irrigation advice needs a current root-zone moisture value. Take a reading (probe or feel test at 15–20 cm) and add it to the farm profile.'))
    else:
        plan = irrigation_plan(farm)
        status, target, deficit = moisture_status(m, stage)
        if plan and ('irrigation', None) not in done and deficit > 3:
            sev = 'high' if deficit > 25 else ('medium' if deficit > 10 else 'low')
            when = 'Irrigate today' if sev != 'low' else 'Top up irrigation soon'
            sess = f" in {plan['sessions']} session{'s' if plan['sessions'] > 1 else ''}" if plan['sessions'] > 1 else ''
            recs.append(dict(kind='irrigation', severity=sev, product=None,
                title=f"{when}: about {plan['kl']:,} kL ({plan['hours']:g} h of pumping){sess}",
                volume_kl=plan['kl'], pump_hours=plan['hours'], deficit_pct=plan['deficit'],
                reason=(f"Soil moisture on {name} is {m:g}% against a {target:g}% target for the {stage} stage — a {plan['deficit']:g}% shortfall. "
                        f"Refilling the root zone at {int(METHOD_EFF.get(plan['method'], .75) * 100)}% {plan['method'].lower()} efficiency needs about {plan['depth_mm']:g} mm of water, "
                        f"i.e. roughly {plan['kl']:,} kL across {area:g} acres. At an assumed pump rate of {PUMP_KL_PER_H:g} kL/h that is about {plan['hours']:g} h"
                        + (f", split into {plan['sessions']} sessions of under {SESSION_MAX_H} h to limit runoff." if plan['sessions'] > 1 else '.'))))
        elif status == 'wet':
            recs.append(dict(kind='alert', severity='medium', product=None,
                title=f'Hold irrigation on {name} — soil is very wet',
                reason=f'Moisture of {m:g}% is more than 10 points above the {target:g}% target for the {stage} stage. Skip irrigation, check field drains, and watch for waterlogging on {soil.lower()} soil.'))

    # ---- Fertilizer by growth stage ---------------------------------------
    if area > 0:
        for product, per_acre, why in FERT_PLAN.get(stage, []):
            if ('fertilizer', product) in done:
                continue
            kg = round(per_acre * area)
            recs.append(dict(kind='fertilizer', severity='medium', product=product, quantity_kg=kg,
                title=f'Apply {kg:,} kg {product} on {name}',
                reason=(f'{name} is in the {stage} stage. {why} Guideline rate is {per_acre} kg/acre × {area:g} acres = {kg:,} kg. '
                        'Confirm against a soil test and apply with irrigation or just before rain.')))

    # ---- Soil pH ------------------------------------------------------------
    if ph is not None:
        if ph < 5.5:
            recs.append(dict(kind='alert', severity='high', product=None,
                title=f'Soil on {name} is acidic (pH {ph:g})',
                reason='Below pH 5.5 nutrient uptake and root growth suffer. Confirm with a lab soil test and ask your agriculture office about liming before the next fertilizer round.'))
        elif ph > 8.0:
            recs.append(dict(kind='alert', severity='high', product=None,
                title=f'Soil on {name} is alkaline (pH {ph:g})',
                reason='Above pH 8 micronutrients such as zinc and iron become less available. Confirm with a soil test; gypsum and organic matter are common corrections.'))

    # ---- Disease flag from the latest field check ---------------------------
    if last_advisory and str(last_advisory.get('disease_risk', '')).lower() == 'high':
        recs.append(dict(kind='alert', severity='high', product=None,
            title=f'High disease-screening risk recorded for {name}',
            reason='Your latest field check flagged high humidity and temperature. Scout rows for red rot, rust or leaf scald symptoms and avoid overhead irrigation late in the day.'))

    recs.sort(key=lambda r: SEVERITY_ORDER.get(r['severity'], 3))
    return recs


def time_ago(secs):
    """Compact relative time from an age in seconds: '10 h ago', '4 d ago'."""
    if secs is None:
        return ''
    secs = max(0, int(secs))
    if secs < 90:
        return 'just now'
    if secs < 3600:
        return f'{secs // 60} min ago'
    if secs < 86400:
        return f'{secs // 3600} h ago'
    if secs < 86400 * 60:
        return f'{secs // 86400} d ago'
    return f'{secs // (86400 * 30)} mo ago'
