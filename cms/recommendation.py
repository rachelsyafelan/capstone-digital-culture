from .models import Event, News, VisitorLog
from django.utils.timezone import now
import re


def normalize_engagement_score(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0

    if value <= 1:
        value *= 100

    return max(0, min(100, round(value)))


# =====================================
# HELPER: Ekstrak event_id dari URL
# =====================================

def _extract_event_id_from_path(page_visited: str):
    """
    Ekstrak event_id dari path seperti:
    - 'event_detail/15'
    - '/events/detail/15'
    """
    match = re.search(r'(\d+)$', page_visited or '')
    return int(match.group(1)) if match else None


# =====================================
# HELPER: Bangun fitur dari VisitorLog
# =====================================

def _build_visitor_features():
    """
    Agregasi VisitorLog per event_id:
    {
        event_id: {
            'visit_count': int,
            'avg_duration': float,
            'avg_engagement': float,
        }
    }
    """
    logs = VisitorLog.objects.filter(
        page_visited__iregex=r'(event_detail|events/detail)/\d+'
    )

    features = {}
    for log in logs:
        event_id = _extract_event_id_from_path(log.page_visited)
        if not event_id:
            continue

        if event_id not in features:
            features[event_id] = {
                'visit_count': 0,
                'total_duration': 0,
                'total_engagement': 0,
                'count': 0,
            }

        features[event_id]['visit_count'] += 1
        features[event_id]['total_duration'] += float(log.visit_duration or 0)
        features[event_id]['total_engagement'] += normalize_engagement_score(log.engagement_score)
        features[event_id]['count'] += 1

    # Hitung rata-rata
    result = {}
    for event_id, data in features.items():
        count = data['count'] or 1
        result[event_id] = {
            'visit_count': data['visit_count'],
            'avg_duration': data['total_duration'] / count,
            'avg_engagement': data['total_engagement'] / count,
        }

    return result


# =====================================
# ML SCORING (Linear Regression Manual)
# =====================================

def _get_candidate_events(core_name=None):
    qs = Event.objects.filter(
        status__in=[
            Event.STATUS_PUBLISHED,
            Event.STATUS_ONGOING,
            Event.STATUS_COMPLETED,
        ]
    )
    if core_name:
        qs = qs.filter(core_values__core_value_name__iexact=core_name)
    return qs.distinct()


def _train_weights(visitor_features: dict, events):
    """
    Hitung bobot dari data VisitorLog secara sederhana:
    - Normalisasi setiap fitur ke skala 0-1
    - Bobot dipelajari dari korelasi engagement dengan fitur lain
    Fallback ke bobot default kalau data tidak cukup.
    """
    try:
        from sklearn.linear_model import LinearRegression
        import numpy as np

        X, y = [], []

        for event in events:
            if not event.event_date:
                continue

            eid = event.event_id
            vf = visitor_features.get(eid, {})

            rating = float(getattr(event, 'rating', 0) or 0)
            days = (now().date() - event.event_date).days
            recency = max(0, min(1, 1 - days / 365))
            visit_count = float(vf.get('visit_count', 0))
            avg_duration = float(vf.get('avg_duration', 0))
            avg_engagement = float(vf.get('avg_engagement', 0))

            if avg_engagement > 0:
                X.append([rating, recency, visit_count, avg_duration])
                y.append(avg_engagement)

        if len(X) < 3:
            return None

        model = LinearRegression()
        model.fit(np.array(X), np.array(y))
        return model

    except ImportError:
        return None


# =====================================
# CALCULATE SCORE (ML atau Rule-based)
# =====================================

def calculate_event_score(event, visitor_features=None, ml_model=None):
    """
    Scoring hybrid:
    - Kalau ML model tersedia → pakai prediksi ML
    - Kalau tidak → fallback ke rule-based
    """
    if visitor_features is None:
        visitor_features = {}

    eid = event.event_id
    vf = visitor_features.get(eid, {})

    rating = float(getattr(event, 'rating', 0) or 0)
    if event.event_date:
        days = (now().date() - event.event_date).days
    else:
        days = 365
    recency = max(0, min(1, 1 - days / 365))
    visit_count = float(vf.get('visit_count', 0))
    avg_duration = float(vf.get('avg_duration', 0))

    if ml_model is not None:
        try:
            import numpy as np
            features = np.array([[rating, recency, visit_count, avg_duration]])
            score = float(ml_model.predict(features)[0])
            return score
        except Exception:
            pass

    score = 0
    score += rating * 0.5

    if days <= 7:
        recency_score = 10
    elif days <= 30:
        recency_score = 7
    else:
        recency_score = 4
    score += recency_score * 0.3

    score += min(visit_count * 0.1, 2.0)
    score += min(avg_duration * 0.01, 1.0)

    return score


# =====================================
# RECOMMENDED EVENTS
# =====================================

def get_recommended_events(limit=3, core_name=None):
    events = _get_candidate_events(core_name)

    if not events.exists():
        return []

    visitor_features = _build_visitor_features()
    ml_model = _train_weights(visitor_features, events)

    scored_events = []
    for event in events:
        try:
            score = calculate_event_score(event, visitor_features, ml_model)
            scored_events.append((score, event))
        except Exception:
            continue

    if not scored_events:
        return list(events.order_by("-event_date")[:limit])

    scored_events.sort(key=lambda x: x[0], reverse=True)
    return [event for _, event in scored_events[:limit]]


# =====================================
# LATEST NEWS (SAFE)
# =====================================

def get_latest_news(limit=3):
    return News.objects.order_by("-created_at")[:limit]