from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import send_mail
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.core.paginator import Paginator
from django.conf import settings
from django.utils import timezone
from django.db.models.functions import TruncDay, TruncMonth, TruncQuarter, TruncYear
import json, uuid
from datetime import date, datetime, timedelta, time
import os
from groq import Groq
import unicodedata
import logging
from django.db.models import Avg, Q, Count, Max, F
from django.db.models import OuterRef, Exists

logger = logging.getLogger(__name__)
from .models import (
    Event,
    News,
    Attribute,
    CoreValue,
    NewsEdition,
    Feedback,
    AdminActivity,
    VisitorLog,
    EventCoreValue,
    EventRegistration,
    Attendance,
)
from .recommendation import (get_recommended_events, get_latest_news, normalize_engagement_score)
from .sentiment import sentiment_analyzer
from .forms import EventRegistrationForm, get_missing_required_fields_for_publish


def _parse_admin_datetime(raw_value, field_label, errors):
    """
    Parse a datetime-local input from an admin form (registration_deadline,
    attendance_open_time, attendance_close_time). Returns a timezone-aware
    datetime, None (if the field was left blank), or None while appending a
    human-readable message to `errors` if the value couldn't be parsed —
    so the admin sees a friendly form error instead of a raw 500.
    """
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed
    except (TypeError, ValueError):
        errors.append(f"Format '{field_label}' tidak valid. Gunakan format tanggal & waktu yang benar.")
        return None


def normalize_email_for_compare(email):
    """Normalize email for duplicate checking: trim, unicode-normalize, remove invisible characters, lower-case."""
    if not email:
        return ''
    # Normalize unicode forms and strip surrounding whitespace
    e = unicodedata.normalize('NFKC', email).strip()
    # Remove common invisible characters (zero-width space, BOM)
    for ch in ('\u200b', '\u200c', '\u200d', '\ufeff'):
        e = e.replace(ch, '')
    # Collapse internal whitespace and lower-case
    e = ' '.join(e.split()).lower()
    return e


def _aware_dt(event_date, t):
    """Combine a date + time into a tz-aware datetime in the current timezone."""
    dt = datetime.combine(event_date, t)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def auto_update_event_statuses():
    """Auto-transition event lifecycle status based on the current time.

    This is a lightweight, idempotent update intended to run at view entry
    (e.g. explore()) so the public listing always reflects reality, without
    any admin action required:

      published -> ongoing    once event_date/event_time has started
      ongoing   -> completed  once event_date/end_time (or event_time, or
                               end-of-day as a last resort) has passed

    `draft` and `archived` are never touched here — those are exclusively
    admin-controlled via the CRUD screens.
    """
    now = timezone.now()

    # ── published -> ongoing ──────────────────────────────────────
    to_ongoing = Event.objects.filter(status=Event.STATUS_PUBLISHED, event_date__isnull=False)
    updated_ongoing = []
    for ev in to_ongoing:
        try:
            start_time = ev.event_time if ev.event_time is not None else time(0, 0)
            start_dt = _aware_dt(ev.event_date, start_time)
            if now >= start_dt:
                ev.status = Event.STATUS_ONGOING
                ev.save(update_fields=['status'])
                updated_ongoing.append(ev.event_id)
        except Exception:
            continue
    if updated_ongoing:
        logger.info('Auto-updated events to ongoing: %s', updated_ongoing)

    # ── ongoing -> completed ──────────────────────────────────────
    to_completed = Event.objects.filter(status=Event.STATUS_ONGOING, event_date__isnull=False)
    updated_completed = []
    for ev in to_completed:
        try:
            if ev.end_time is not None:
                end_time = ev.end_time
            elif ev.event_time is not None:
                end_time = ev.event_time
            else:
                end_time = time(23, 59, 59)
            end_dt = _aware_dt(ev.event_date, end_time)
            if now >= end_dt:
                ev.status = Event.STATUS_COMPLETED
                ev.save(update_fields=['status'])
                updated_completed.append(ev.event_id)
        except Exception:
            continue
    if updated_completed:
        logger.info('Auto-updated events to completed: %s', updated_completed)


# =====================
# ADMIN AUTH HELPER
# =====================
def admin_login_required(view_func):
    """Decorator: redirect ke login page kalau belum login sebagai admin."""
    def wrapper(request, *args, **kwargs):
        if not request.session.get('is_admin'):
            return redirect('admin_login')
        return view_func(request, *args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper


# =====================
# ADMIN LOGIN / LOGOUT
# =====================
# Single canonical admin_login (used by url 'admin_login')
def admin_login(request):
    # The login modal on home.html submits via fetch() and expects a JSON
    # response ({'error': ...} or {'redirect': ...}) — not a normal Django
    # render/redirect. Without this, a wrong password used to render a 200
    # HTML page that the frontend misread as "success" and redirected the
    # user to /admin-home/, which then bounced them straight back with no
    # error ever shown.
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    if request.session.get('is_admin') and request.user.is_authenticated:
        if is_ajax:
            return JsonResponse({'redirect': '/admin-home/'})
        return redirect('admin_home')
    elif request.session.get('is_admin'):
        # session is_admin ada tapi auth Django-nya nggak valid — bersihkan
        request.session.flush()
    error = None
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()

        user = authenticate(request, username=username, password=password)
        if user is not None and (user.is_staff or user.is_superuser):
            login(request, user)
            request.session['is_admin'] = True
            request.session['admin_username'] = user.username
            if is_ajax:
                return JsonResponse({'redirect': '/admin-home/'})
            return redirect('admin_home')
        else:
            error = 'Username atau password salah, atau akun tidak memiliki akses admin.'
            if is_ajax:
                return JsonResponse({'error': error})

    if is_ajax:
        return JsonResponse({'error': 'Metode request tidak valid.'}, status=400)
    return render(request, 'cms/admin_login.html', {'error': error})


def admin_forgot_password(request):
    if request.session.get('is_admin'):
        return redirect('admin_home')

    success = False
    error = None
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if not email:
            error = 'Alamat email wajib diisi.'
        else:
            user = User.objects.filter(email__iexact=email).first()
            if user and (user.is_staff or user.is_superuser) and user.is_active:
                uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
                token = default_token_generator.make_token(user)
                reset_path = f"/admin-reset-password/{uidb64}/{token}/"
                reset_link = request.build_absolute_uri(reset_path)

                subject = 'Reset Password Admin Digital Culture'
                message = (
                    f'Halo {user.username},\n\n'
                    'Kami menerima permintaan reset password untuk akun admin Digital Culture.\n'
                    'Klik link di bawah ini untuk membuat password baru:\n\n'
                    f'{reset_link}\n\n'
                    'Link ini hanya berlaku sekali pakai dan akan kedaluwarsa demi keamanan.\n'
                    'Jika Anda tidak pernah mengajukan permintaan ini, abaikan email ini — '
                    'password Anda tidak akan berubah.'
                )
                # fail_silently=True di parameter send_mail tetap dipakai supaya
                # response ke user tidak bocorin status pengiriman (anti
                # user-enumeration). TAPI errornya sekarang di-log manual di
                # server supaya bisa didiagnosis dari terminal/log Railway,
                # bukan ketelen tanpa jejak sama sekali.
                try:
                    sent_count = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
                    if sent_count:
                        logger.info("Forgot-password email terkirim ke user_id=%s", user.pk)
                    else:
                        logger.warning("send_mail return 0 (tidak ada email terkirim) untuk user_id=%s", user.pk)
                except Exception:
                    logger.exception("Gagal kirim forgot-password email untuk user_id=%s", user.pk)
            else:
                logger.info(
                    "Forgot-password: email '%s' tidak match ke admin manapun (atau tidak aktif/bukan staff).",
                    email,
                )
            # `success = True` selalu di-set terlepas email ditemukan atau
            # tidak, supaya orang tidak bisa menebak-nebak email admin mana
            # yang valid dari respons halaman ini (anti user-enumeration).
            success = True

    return render(request, 'cms/admin_forgot_password.html', {'success': success, 'error': error})


def admin_reset_password(request, uidb64, token):
    """
    Halaman set password baru, diakses lewat link unik di email forgot
    password. Token dari `default_token_generator` otomatis invalid
    setelah dipakai sekali (karena tergantung hash password saat ini —
    begitu password diganti, token lama otomatis tidak valid lagi) dan
    juga expired otomatis setelah PASSWORD_RESET_TIMEOUT (default 3 hari).
    """
    if request.session.get('is_admin'):
        return redirect('admin_home')

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    token_valid = user is not None and default_token_generator.check_token(user, token)

    if not token_valid:
        return render(request, 'cms/admin_reset_password.html', {
            'token_valid': False,
        })

    error = None
    if request.method == 'POST':
        password1 = request.POST.get('password1', '')
        password2 = request.POST.get('password2', '')

        if not password1 or not password2:
            error = 'Kedua kolom password wajib diisi.'
        elif password1 != password2:
            error = 'Konfirmasi password tidak cocok.'
        else:
            from django.contrib.auth.password_validation import validate_password, ValidationError
            try:
                validate_password(password1, user=user)
            except ValidationError as e:
                error = ' '.join(e.messages)

        if not error:
            user.set_password(password1)
            user.save()
            return render(request, 'cms/admin_reset_password.html', {
                'token_valid': True,
                'success': True,
            })

    return render(request, 'cms/admin_reset_password.html', {
        'token_valid': True,
        'error': error,
    })


# Single canonical admin_logout (used by url 'admin_logout')
def admin_logout_view(request):
    request.session.flush()
    logout(request)
    return redirect('admin_login')


# =====================
# USER PAGES
# =====================
def home(request):
    # NOTE: visit_duration/engagement_score aren't known yet at request time
    # (they depend on how long/how the visitor actually engages with the
    # page). We no longer fabricate constant values here — real numbers are
    # recorded via track_activity() once the frontend reports actual
    # behavior (time on page, scroll depth, etc).
    VisitorLog.objects.create(page_visited='home')

    avg_score = VisitorLog.objects.aggregate(
        avg=Avg('engagement_score')
    )['avg'] or 0
    engagement_score = normalize_engagement_score(avg_score)

    context = {
        # Only count Published events in public stats
        "total_events": Event.objects.filter(status=Event.STATUS_PUBLISHED).count(),
        "total_news": News.objects.count(),
        "total_attributes": Attribute.objects.count(),
        "core_values": CoreValue.objects.all(),
        "recommended_events": get_recommended_events(),
        "latest_news": get_latest_news(),
        "engagement_score": engagement_score,
    }

    return render(request, "cms/home.html", context)


def _recommended_items_for_core_slug(core_slug):
    if not core_slug:
        return []

    core_value = CoreValue.objects.filter(
        core_value_name__iexact=core_slug.replace("-", " ")
    ).first()
    if not core_value:
        return []

    event_ids = EventCoreValue.objects.filter(
        core_value=core_value
    ).values_list("event_id", flat=True)

    events = Event.objects.filter(
        event_id__in=event_ids,
        status=Event.STATUS_PUBLISHED
    ).order_by("-created_at")[:12]

    return [
        {
            "id": e.event_id,
            "title": e.event_name,
            "summary": (e.description or "")[:120],
            "image_url": e.image_url or "",
            "location": e.location or "",
            "event_date": str(e.event_date) if e.event_date else "",
            "rating": float(e.rating) if e.rating is not None else None,
            "category": core_value.core_value_name,
        }
        for e in events
    ]

def get_or_create_session(request):
    session_id = request.COOKIES.get('feedback_session')
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id

BULAN = {
    'januari': 1, 'februari': 2, 'maret': 3, 'april': 4,
    'mei': 5, 'juni': 6, 'juli': 7, 'agustus': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'desember': 12
}

def parse_edition_date(edition):
    name = edition.edition_name.lower().strip()
    return BULAN.get(name, 0)


def explore(request):
    # Keep event lifecycle status fresh (published→ongoing→completed) before
    # querying, so the listing below never shows a stale status.
    auto_update_event_statuses()

    # See home() — real visit_duration/engagement_score are recorded via
    # track_activity() from the frontend, not fabricated here.
    VisitorLog.objects.create(page_visited='explore')

    session_id = get_or_create_session(request)

    # Business Rule: Published, Ongoing, and Completed events show on Explore
    # (matches Event.is_visible_to_participants — ongoing events are the most
    # relevant to visitors and shouldn't disappear from the public listing).
    events_qs = Event.objects.filter(
        status__in=[Event.STATUS_PUBLISHED, Event.STATUS_ONGOING, Event.STATUS_COMPLETED]
    )
    events = list(events_qs)
    today = timezone.localdate()

    def sort_event(e):
        e_date = e.event_date
        e_time_str = e.event_time.strftime('%H:%M:%S') if e.event_time else '00:00:00'

        if not e_date:
            return (2, 0, e_time_str)
        elif e_date >= today:
            return (0, (e_date - today).days, e_time_str)
        else:
            return (1, -(e_date - today).days, e_time_str)

    events.sort(key=sort_event)

    # Event.rating is already kept up to date by save_feedback() whenever a
    # rating is submitted, so it's the single source of truth here — no need
    # to recompute the average per event (that was a duplicate, N+1 query).

    rated_events = list(Feedback.objects.filter(
        session_id=session_id,
        rating__isnull=False
    ).values_list('event_id', flat=True))

    news_editions = sorted(
        NewsEdition.objects.prefetch_related('news').all(),
        key=parse_edition_date,
        reverse=True
    )

    # News tanpa edition (edition_id NULL / edition sudah dihapus) tetap
    # harus tampil di Explore. Tanpa ini, News jenis ini tidak akan pernah
    # muncul karena loop di template jalan per-edition
    # (for edition in news_editions -> for news in edition.news.all).
    news_no_edition = News.objects.filter(edition__isnull=True).order_by('-created_at')

    playbooks = Attribute.objects.filter(attribute_type='playbook')
    posters   = Attribute.objects.filter(attribute_type='poster')
    assets    = Attribute.objects.filter(attribute_type='asset')
    logos     = Attribute.objects.filter(attribute_type='logo')
    videos    = Attribute.objects.filter(attribute_type='video')

    # Annotate events with flags used by template: can_feedback (>=1 hour after start)
    now = timezone.now()
    for e in events:
        e.is_rated_by_session = (e.event_id in rated_events)
        e.can_feedback = False
        if e.event_date:
            ev_time = e.event_time if e.event_time is not None else time(0, 0)
            try:
                ev_dt = datetime.combine(e.event_date, ev_time)
                if timezone.is_naive(ev_dt):
                    ev_dt = timezone.make_aware(ev_dt, timezone.get_current_timezone())
                e.can_feedback = now >= (ev_dt + timedelta(hours=1))
            except Exception:
                e.can_feedback = (e.event_date < date.today())

    context = {
        'events': events,
        'news_editions': news_editions,
        'news_no_edition': news_no_edition,
        'playbooks': playbooks,
        'posters': posters,
        'assets': assets,
        'logos': logos,
        'videos': videos,
        'today': date.today(),
        'rated_events': rated_events,
    }

    response = render(request, 'cms/explore.html', context)
    response.set_cookie(
        'feedback_session', session_id,
        max_age=60 * 60 * 24 * 30,
        httponly=True, samesite='Lax'
    )
    return response

def feedback(request):
    return render(request, 'cms/feedback.html')


def classify_feedback(message):
    try:
        sentiment, confidence = sentiment_analyzer.predict(message)
    except Exception:
        sentiment = 'neutral'
        confidence = 0.50
    return sentiment, confidence

# =========================================================
# CHAT AI
# =========================================================
@require_http_methods(["POST"])
def chat_with_ai(request):

    try:
        data = json.loads(request.body)

        message  = data.get('message', '').strip()
        event_id = data.get('event_id')

        # ================= VALIDASI =================

        if not message:
            return JsonResponse({
                'status': 'error',
                'message': 'Pesan kosong'
            }, status=400)

        # ================= OPTIONAL EVENT =================
        # event boleh kosong
        event = None

        if event_id:
            try:
                event = Event.objects.get(event_id=event_id)

            except Event.DoesNotExist:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Event tidak valid'
                }, status=404)

        session_id = get_or_create_session(request)

        sentiment, confidence = classify_feedback(message)

        # ================= HISTORY CHAT =================

        previous_feedbacks = Feedback.objects.filter(
            session_id=session_id
        ).exclude(
            ai_response__isnull=True
        ).order_by('-created_at')[:5]

        previous_feedbacks = reversed(previous_feedbacks)

        # ================= CONTEXT DATA (EVENTS & NEWS) =================
        db_events = Event.objects.filter(
            status__in=[Event.STATUS_PUBLISHED, Event.STATUS_ONGOING, Event.STATUS_COMPLETED]
            ).order_by(F('event_date').desc(nulls_last=True))[:5]
        db_news = News.objects.all().order_by('-created_at')[:5]

        events_list = []
        for ev in db_events:
            date_str = ev.event_date.strftime('%d %b %Y') if ev.event_date else 'Tidak ditentukan'
            time_str = ev.event_time.strftime('%H:%M') if ev.event_time else 'Tidak ditentukan'
            desc_str = ev.description[:120] + '...' if ev.description and len(ev.description) > 120 else (ev.description or '')
            events_list.append(
                f"- ID: {ev.event_id}\n"
                f"  Nama: {ev.event_name}\n"
                f"  Tanggal: {date_str}\n"
                f"  Waktu: {time_str}\n"
                f"  Lokasi: {ev.location or 'Tidak ditentukan'}\n"
                f"  Deskripsi: {desc_str}\n"
                f"  Link: /event_detail/{ev.event_id}/"
            )

        news_list = []
        for nw in db_news:
            date_str = nw.created_at.strftime('%d %b %Y') if nw.created_at else 'Tidak ditentukan'
            content_str = nw.content[:120] + '...' if nw.content and len(nw.content) > 120 else (nw.content or '')
            news_list.append(
                f"- ID: {nw.news_id}\n"
                f"  Judul: {nw.title or 'Tanpa Judul'}\n"
                f"  Tanggal: {date_str}\n"
                f"  Ringkasan: {content_str}\n"
                f"  Link: /news_redirect/{nw.news_id}/"
            )

        events_context_str = "\n\n".join(events_list) if events_list else "Tidak ada data event."
        news_context_str = "\n\n".join(news_list) if news_list else "Tidak ada data berita."

        db_attributes = Attribute.objects.all().order_by('-created_at')[:5]
        attributes_list = []
        for attr in db_attributes:
            attributes_list.append(
                f"- ID: {attr.attribute_id}\n"
                f"  Nama: {attr.attribute_name or 'Tanpa Judul'}\n"
                f"  Tipe: {attr.attribute_type or 'Tidak diketahui'}\n"
                f"  Link: /attribute_redirect/{attr.attribute_id}/"
            )
        attributes_context_str = "\n\n".join(attributes_list) if attributes_list else "Tidak ada data attribute."

        total_events = Event.objects.filter(status=Event.STATUS_PUBLISHED).count()
        total_news = News.objects.count()
        total_attributes = Attribute.objects.count()
        avg_event_rating = Event.objects.aggregate(avg=Avg('rating'))['avg'] or 0

        sentiment_counts = {
            'positive': 0,
            'neutral': 0,
            'negative': 0,
        }
        for row in Feedback.objects.filter(is_genuine_feedback=True).values('sentiment').annotate(count=Count('feedback_id')):
            sentiment_counts[row['sentiment']] = row['count']

        events_per_core = EventCoreValue.objects.values(
            'core_value__core_value_name'
        ).annotate(count=Count('event', distinct=True)).order_by('-count')[:5]
        top_core_values = []
        for row in events_per_core:
            top_core_values.append(
                f"- {row['core_value__core_value_name']}: {row['count']} event"
            )
        top_core_values_str = "\n".join(top_core_values) if top_core_values else "- Tidak ada core value tersedia."

        culture_context_str = (
            f"Ringkasan Culture Performance:\n"
            f"- Total event dipublikasikan: {total_events}\n"
            f"- Total berita: {total_news}\n"
            f"- Total attribute: {total_attributes}\n"
            f"- Rata-rata rating event: {round(avg_event_rating, 1)}\n"
            f"- Sentimen feedback: {sentiment_counts['positive']} positif, {sentiment_counts['neutral']} netral, {sentiment_counts['negative']} negatif\n"
            f"- Top core values berdasarkan jumlah event:\n"
            f"{top_core_values_str}"
        )

        current_event_context = ""
        if event:
            current_event_context = (
                f"User saat ini sedang membuka halaman event berikut:\n"
                f"- Nama: {event.event_name}\n"
                f"- Deskripsi: {event.description or 'Tidak ada'}\n"
                f"- Tanggal: {event.event_date.strftime('%d %b %Y') if event.event_date else 'Tidak ditentukan'}\n"
                f"- Lokasi: {event.location or 'Tidak ada'}\n\n"
            )

        # ================= PROMPT =================

        system_content = (
            "Kamu adalah asisten digital Pegadaian bernama Digi.\n\n"

            "Kamu HANYA boleh menjawab topik terkait Pegadaian, seperti:\n"
            "- budaya digital Pegadaian\n"
            "- layanan Pegadaian\n"
            "- aplikasi dan teknologi Pegadaian\n"
            "- event atau seminar Pegadaian\n"
            "- berita atau news Pegadaian\n"
            "- transformasi digital Pegadaian\n"
            "- pengalaman pengguna terhadap Pegadaian\n\n"

            f"{current_event_context}"

            "Berikut adalah data event terbaru/mendatang dari database Pegadaian:\n"
            f"{events_context_str}\n\n"

            "Berikut adalah data berita/news terbaru dari database Pegadaian:\n"
            f"{news_context_str}\n\n"

            "Berikut adalah data attribute terbaru dari website Pegadaian:\n"
            f"{attributes_context_str}\n\n"

            "Berikut adalah ringkasan Culture Performance yang ditampilkan di website Pegadaian:\n"
            f"{culture_context_str}\n\n"

            "Jika user bertanya di luar Pegadaian "
            "(contoh: politik, game, kesehatan, coding umum, hiburan), "
            "tolak dengan sopan dan arahkan kembali ke topik Pegadaian.\n\n"

            "Ketika menjawab pertanyaan tentang event atau berita/news:\n"
            "- Gunakan data di atas untuk memberikan informasi yang akurat.\n"
            "- Berikan detail tanggal, lokasi, dan penjelasan singkat jika ditanyakan.\n"
            "- Sertakan link dalam bentuk tag HTML <a> agar user bisa mengkliknya langsung di chat. FORMAT LINK:\n"
            "  <a href=\"/event_detail/ID_EVENT/\" style=\"color: #00A651; font-weight: bold; text-decoration: underline;\">Nama Event</a>\n"
            "  atau <a href=\"/news_redirect/ID_NEWS/\" style=\"color: #00A651; font-weight: bold; text-decoration: underline;\">Judul Berita</a>\n"
            "- Ganti ID_EVENT atau ID_NEWS dengan ID riil yang sesuai dari data di atas.\n"
            "- PENTING: Gunakan tag HTML <a> persis seperti contoh di atas (dengan inline style warna hijau #00A651 agar kontras dan tebal). Jangan gunakan markdown link seperti [Nama](/link).\n\n"

            "Gunakan bahasa yang sama dengan user.\n"
            "- Indonesia → jawab Indonesia\n"
            "- English → jawab English\n"
            "- Campur → jawab natural\n\n"

            "Aturan:\n"
            "- Maksimal 3 kalimat. Namun, jika kamu memberikan daftar/list event atau berita, batasan 3 kalimat boleh dilonggarkan agar kamu bisa menyusun daftar poin-poin yang rapi, informatif, dan jelas.\n"
            "- Jangan terlalu panjang\n"
            "- Jawaban harus nyambung dengan chat sebelumnya\n"
            "- Ramah dan profesional\n"
            "- Jangan mengulang jawaban yang sama\n"
            "- Gaya modern AI assistant\n"
            "- Jika user memberi kritik → respon empati\n"
            "- Jika user memberi pujian → respon positif singkat\n\n"

            "Di akhir jawaban tambahkan:\n"
            "IS_FEEDBACK:true\n"
            "jika user memberi opini, kritik, pengalaman, atau penilaian.\n\n"

            "IS_FEEDBACK:false\n"
            "jika user hanya bertanya informasi biasa."
        )

        messages = [
            {
                "role": "system",
                "content": system_content
            }
        ]

        # ================= HISTORY =================

        for fb in previous_feedbacks:

            messages.append({
                "role": "user",
                "content": fb.message
            })

            messages.append({
                "role": "assistant",
                "content": fb.ai_response
            })

        # ================= USER MESSAGE =================

        messages.append({
            "role": "user",
            "content": message
        })

        # ================= AI RESPONSE =================

        client = Groq(api_key=settings.GROQ_API_KEY)

        groq_response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=300,
            temperature=0.6,
        )

        full_reply = groq_response.choices[0].message.content.strip()

        # ================= FEEDBACK FLAG =================

        is_feedback = False

        if "IS_FEEDBACK:true" in full_reply:
            is_feedback = True

        reply = (
            full_reply
            .replace("IS_FEEDBACK:true", "")
            .replace("IS_FEEDBACK:false", "")
            .strip()
        )

        # ================= GENUINE FEEDBACK CHECK =================
        # Setiap pesan tetap disimpan (supaya previous_feedbacks/history chat
        # jalan terus), tapi hanya ditandai is_genuine_feedback=True kalau:
        #   a. LLM benar-benar mendeteksi ini feedback (is_feedback == True), DAN
        #   b. kalau pesan terkait event (event_id ada), pengirimnya sudah
        #      attendance-verified untuk event itu — pakai helper yang sama
        #      dengan Step 7 Anonymous Feedback System
        #      (Feedback.email_has_verified_attendance), dicek dari email
        #      yang tersimpan di session (session key sama yang dipakai
        #      submit_attendance() / _get_participation_context()).
        # Asumsi: tanpa event terkait, syarat attendance otomatis terpenuhi
        # (feedback umum soal budaya/aplikasi tidak mensyaratkan kehadiran).
        participant_email = (request.session.get('participant_email') or '').strip().lower()

        attendance_ok = True
        if event:
            attendance_ok = Feedback.email_has_verified_attendance(participant_email, event)

        is_genuine_feedback = bool(is_feedback and attendance_ok)

        # ================= SAVE DB =================

        fb = Feedback.objects.create(
            session_id=session_id,
            event=event,
            participant_email=participant_email or None,
            message=message,
            ai_response=reply,
            sentiment=sentiment if is_genuine_feedback else None,
            rating=None,
            source_platform='web',
            is_genuine_feedback=is_genuine_feedback,
            is_feedback_detected=is_feedback,
        )

        # ================= CEK RATING =================

        show_rating = False

        # rating hanya muncul kalau:
        # 1. feedback
        # 2. ada event
        # 3. event sudah berjalan setidaknya 1 jam (bukan sebelum dimulai)
        if is_feedback and event:

            already_rated = Feedback.objects.filter(
                session_id=session_id,
                event=event,
                rating__isnull=False
            ).exists()

            show_rating = False
            if event.event_date:
                # Build event start datetime (use midnight if time not provided)
                ev_time = event.event_time if event.event_time is not None else time(0, 0)
                try:
                    ev_dt = datetime.combine(event.event_date, ev_time)
                    if timezone.is_naive(ev_dt):
                        ev_dt = timezone.make_aware(ev_dt, timezone.get_current_timezone())
                    # Show rating if at least 1 hour has passed since event start
                    show_rating = (timezone.now() >= ev_dt + timedelta(hours=1)) and not already_rated
                except Exception:
                    # Fallback: if something goes wrong, keep previous behavior (show only after event day)
                    show_rating = (event.event_date < date.today()) and not already_rated

        # ================= RESPONSE =================

        response = JsonResponse({
            'status': 'success',
            'reply': reply,
            'feedback_id': fb.feedback_id,
            'sentiment': sentiment,
            'confidence': round(confidence * 100),
            'show_rating': show_rating
        })

        response.set_cookie(
            'feedback_session',
            session_id,
            max_age=60*60*24*30,
            httponly=True,
            samesite='Lax'
        )

        return response

    except Exception as e:
        # Log detail lengkap di server untuk debugging — JANGAN dikirim ke
        # user, karena bisa membocorkan detail internal (path file, query
        # database, API key error, dll).
        logger.exception('chat_with_ai gagal memproses pesan')

        return JsonResponse({
            'status': 'error',
            'message': 'Terjadi kesalahan pada server. Silakan coba lagi beberapa saat.'
        }, status=500)

# =========================================================
# SAVE RATING
# =========================================================

@require_http_methods(["POST"])
def save_feedback(request):

    try:
        data = json.loads(request.body)

        feedback_id = data.get('feedback_id')
        rating      = data.get('rating')

        if not feedback_id:
            return JsonResponse({
                'status': 'error',
                'message': 'Feedback ID tidak ditemukan'
            }, status=400)

        if not rating:
            return JsonResponse({
                'status': 'error',
                'message': 'Rating wajib diisi'
            }, status=400)

        rating = int(rating)

        if rating < 1 or rating > 5:
            return JsonResponse({
                'status': 'error',
                'message': 'Rating harus 1-5'
            }, status=400)

        session_id = get_or_create_session(request)

        fb = Feedback.objects.get(
            feedback_id=feedback_id
        )

        # ================= CEK SESSION =================
        if fb.session_id != session_id:
            return JsonResponse({
                'status': 'error',
                'message': 'Session tidak valid'
            }, status=403)

        # ================= CEK SUDAH RATING =================
        if fb.rating is not None:
            return JsonResponse({
                'status': 'error',
                'message': 'Rating sudah pernah diberikan'
            }, status=400)

        # ================= UPDATE RATING =================
        fb.rating = rating

        # ================= RECALCULATE SENTIMENT =================
        # Sentimen sebelumnya dihitung dari teks saja (rating belum ada saat
        # feedback dibuat via chatbot). Sekarang rating sudah tersedia, hitung
        # ulang dengan sentiment_analyzer.predict(message, rating) supaya
        # bintang yang diberikan ikut memengaruhi label akhir, dan hasilnya
        # konsisten dengan yang dipakai di feedback_admin() & culture_performance().
        try:
            new_sentiment, new_confidence = sentiment_analyzer.predict(fb.message, rating)
            fb.sentiment = new_sentiment
        except Exception:
            # Kalau prediksi gagal, biarkan sentimen lama (dari saat feedback dibuat)
            pass

        fb.save()

        # ================= UPDATE AVG EVENT =================
        avg_rating = Feedback.objects.filter(
            event=fb.event,
            rating__isnull=False,
            is_genuine_feedback=True,
        ).aggregate(avg=Avg('rating'))['avg']

        fb.event.rating = round(avg_rating, 1)
        fb.event.save()

        return JsonResponse({
            'status': 'success',
            'message': 'Rating berhasil disimpan',
            'avg_rating': fb.event.rating
        })

    except Feedback.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': 'Feedback tidak ditemukan'
        }, status=404)

    except Exception as e:
        import traceback
        traceback.print_exc()

        return JsonResponse({
            'status': 'error',
            'message': str(e)
        }, status=500)


def culture_performance(request):
    """
    Was previously a bare render() with no context (empty placeholder).
    Now surfaces real aggregate data: events per core value, and overall
    feedback sentiment/rating breakdown, which "Culture Performance" is the
    natural place to report.
    """
    events_per_core_value = (
        EventCoreValue.objects
        .values('core_value__core_value_name')
        .annotate(event_count=Count('event', distinct=True))
        .order_by('-event_count')
    )

    sentiment_breakdown = (
        Feedback.objects
        .filter(is_genuine_feedback=True)
        .values('sentiment')
        .annotate(count=Count('feedback_id'))
        .order_by('-count')
    )

    avg_event_rating = Event.objects.aggregate(avg=Avg('rating'))['avg']

    context = {
        'core_values': CoreValue.objects.all(),
        'events_per_core_value': events_per_core_value,
        'sentiment_breakdown': sentiment_breakdown,
        'avg_event_rating': round(avg_event_rating, 1) if avg_event_rating else None,
        'total_feedback': Feedback.objects.filter(is_genuine_feedback=True).count(),
    }
    return render(request, 'cms/culture_performance.html', context)


def business_performance(request):
    """
    Was previously a bare render() with no context (empty placeholder).
    Now surfaces real traffic/registration numbers as a starting point for
    a "Business Performance" view.
    """
    context = {
        'total_visits': VisitorLog.objects.count(),
        'total_registrations': EventRegistration.objects.count(),
        'total_events': Event.objects.count(),
        'active_events': Event.objects.filter(
            status__in=[Event.STATUS_PUBLISHED, Event.STATUS_ONGOING]
        ).count(),
        'avg_engagement_score': normalize_engagement_score(
            VisitorLog.objects.aggregate(avg=Avg('engagement_score'))['avg'] or 0
        ),
    }
    return render(request, 'cms/business_performance.html', context)


# =====================
# EVENT DETAIL
# =====================
def _get_participation_context(request, event):
    """
    Tentukan email peserta yang "sedang teridentifikasi" di browser ini
    (lewat session, hasil registrasi/attendance/cek-status sebelumnya),
    lalu hitung status partisipasinya untuk event ini.

    Mengembalikan dict siap pakai untuk context template:
        {
            'participant_email': str | None,
            'participation': {'code': str, 'label': str} | None,
            'is_already_registered': bool,
        }
    """
    email = (request.session.get('participant_email') or '').strip().lower()

    participation = None
    is_already_registered = False

    if email:
        reg = EventRegistration.objects.filter(
            event_id=event.event_id,
            email__iexact=email,
        ).first()
        if reg is not None:
            is_already_registered = True
            participation = reg.get_participation_status()

    return {
        'participant_email': email or None,
        'participation': participation,
        'is_already_registered': is_already_registered,
    }


def event_detail(request, id):
    event = get_object_or_404(Event, event_id=id)

    # Business Rule: Archived events only for admin
    if event.status == Event.STATUS_ARCHIVED:
        if not request.session.get('is_admin'):
            return redirect('explore')

    # Business Rule: Draft/Published only — participants can still see Completed
    if event.status == Event.STATUS_DRAFT:
        if not request.session.get('is_admin'):
            return redirect('explore')

    VisitorLog.objects.create(
        page_visited=f'event_detail/{id}',
        visit_duration=0,
        engagement_score=50,
        visitor_ip=request.META.get('REMOTE_ADDR', ''),
    )

    session_id = get_or_create_session(request)
    rated_events = list(Feedback.objects.filter(
        session_id=session_id,
        rating__isnull=False
    ).values_list('event_id', flat=True))

    now = timezone.now()
    event.is_rated_by_session = (event.event_id in rated_events)
    event.can_feedback = False
    if event.event_date:
        ev_time = event.event_time if event.event_time is not None else time(0, 0)
        try:
            ev_dt = datetime.combine(event.event_date, ev_time)
            if timezone.is_naive(ev_dt):
                ev_dt = timezone.make_aware(ev_dt, timezone.get_current_timezone())
            event.can_feedback = now >= (ev_dt + timedelta(hours=1))
        except Exception:
            event.can_feedback = (event.event_date < date.today())

    context = {'event': event}
    context.update(_get_participation_context(request, event))
    response = render(request, 'cms/event_detail.html', context)
    response.set_cookie(
        'feedback_session', session_id,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite='Lax'
    )
    return response


def check_participation_status(request, id):
    """
    Form kecil "Cek Status Pendaftaran": peserta mengetik email,
    sistem menyimpan email itu ke session (supaya halaman Event Detail
    bisa menampilkan status partisipasi tanpa perlu login), lalu
    redirect balik ke Event Detail.
    """
    event = get_object_or_404(Event, event_id=id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if request.method != "POST":
        return redirect('event_detail', id=event.event_id)

    # "Bukan saya / ganti email": hapus identitas peserta dari session.
    if request.POST.get('clear') == '1':
        request.session.pop('participant_email', None)
        if is_ajax:
            return JsonResponse({"status": "success", "cleared": True})
        return redirect('event_detail', id=event.event_id)

    email = (request.POST.get('email') or '').strip().lower()
    if not email:
        msg = "Email wajib diisi untuk mengecek status pendaftaran."
        if is_ajax:
            return JsonResponse({"status": "error", "errors": [msg]}, status=400)
        return redirect('event_detail', id=event.event_id)

    reg = EventRegistration.objects.filter(
        event_id=event.event_id,
        email__iexact=email,
    ).first()

    if reg is None:
        msg = "Email ini belum pernah mendaftar pada event ini."
        if is_ajax:
            return JsonResponse({"status": "error", "errors": [msg]}, status=404)
        return redirect('event_detail', id=event.event_id)

    # Simpan ke session supaya halaman ini (dan kunjungan berikutnya di
    # browser yang sama) otomatis tahu siapa peserta yang sedang melihat.
    request.session['participant_email'] = email

    if is_ajax:
        participation = reg.get_participation_status()
        return JsonResponse({
            "status": "success",
            "participation_code": participation['code'],
            "participation_label": participation['label'],
        })

    return redirect('event_detail', id=event.event_id)


def _format_event_date(event):
    if not event.event_date:
        return "-"
    months = [
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    ]
    d = event.event_date
    return f"{d.day} {months[d.month - 1]} {d.year}"


def register_event(request, id):
    event = get_object_or_404(Event, event_id=id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Business Rule: registration only for Published events within deadline
    if not event.is_registration_open:
        msg = "Registrasi untuk event ini sudah ditutup atau belum dibuka."
        if is_ajax:
            return JsonResponse({"status": "error", "errors": [msg]}, status=400)
        return render(request, "cms/event_detail.html", {"event": event, "errors": [msg]})

    # Check capacity
    if event.is_full:
        msg = "Kuota event ini sudah penuh."
        if is_ajax:
            return JsonResponse({"status": "error", "errors": [msg]}, status=400)
        return render(request, "cms/event_detail.html", {"event": event, "errors": [msg]})

    if request.method == "POST":
        data = request.POST.copy()
        if not data.get("full_name") and data.get("nama"):
            data["full_name"] = data.get("nama")
        if not data.get("organization") and data.get("instansi"):
            data["organization"] = data.get("instansi")

        # Validasi backend eksplisit: tolak kalau email ini SUDAH terdaftar
        # untuk event ini. Ini lapisan pertahanan tambahan di server —
        # tombol "Daftar" di frontend juga disembunyikan untuk peserta yang
        # sudah teridentifikasi terdaftar, tapi validasi ini tetap berjalan
        # di backend terlepas dari apa yang dikirim klien.
        submitted_email = normalize_email_for_compare(data.get("email") or "")
        if submitted_email:
            already_registered = EventRegistration.objects.filter(
                event_id=event.event_id,
                email__iexact=submitted_email,
            ).exists()
            if already_registered:
                errors = ["Email ini sudah terdaftar untuk event ini."]
                if is_ajax:
                    return JsonResponse({"status": "error", "errors": errors}, status=400)
                return render(
                    request, "cms/event_detail.html",
                    {"event": event, "errors": errors, "open_register_modal": True},
                )

        form = EventRegistrationForm(data, event=event)

        if not form.is_valid():
            errors = [msg for field_errors in form.errors.values() for msg in field_errors]
            if is_ajax:
                return JsonResponse({"status": "error", "errors": errors}, status=400)
            return render(
                request, "cms/event_detail.html",
                {"event": event, "errors": errors, "form": form, "open_register_modal": True},
            )

        reg = form.save(commit=False)
        # Ensure saved email matches the same normalization used for checks
        if reg.email:
            reg.email = normalize_email_for_compare(reg.email)
        reg.event_id = event.event_id
        reg.event_name = event.event_name
        try:
            reg.save()
        except IntegrityError:
            errors = ["Email ini sudah terdaftar untuk event ini."]
            if is_ajax:
                return JsonResponse({"status": "error", "errors": errors}, status=400)
            return render(
                request, "cms/event_detail.html",
                {"event": event, "errors": errors, "form": form, "open_register_modal": True},
            )

        # Simpan email ke session supaya halaman Event Detail langsung tahu
        # status partisipasi peserta ini begitu mereka kembali / refresh.
        request.session['participant_email'] = reg.email

        payload = {
            "status": "success",
            "event_name": event.event_name,
            "event_date": _format_event_date(event),
        }
        if is_ajax:
            return JsonResponse(payload)

        return render(
            request, "cms/event_detail.html",
            {"event": event, "show_success": True, **payload},
        )

    return redirect("event_detail", id=event.event_id)


def success_page(request):
    event_name = request.session.pop("last_registration_event", None)
    return render(request, "cms/success.html", {"event_name": event_name})


# =====================
# API
# =====================
def recommended_content_api(request):
    core_slug = (request.GET.get("core") or "").strip()
    return JsonResponse({"items": _recommended_items_for_core_slug(core_slug)})


@csrf_exempt
@require_http_methods(["POST"])
def track_activity(request):
    """
    Real engagement tracking endpoint. Expects the frontend to report actual
    measured behavior after the visitor is done with a page, e.g.:
        { "page": "home", "visit_duration": 47, "engagement_score": 0.62 }
    This replaces the old stub that just printed the payload and discarded
    it — engagement numbers shown in the admin dashboard now come from here
    rather than being hardcoded constants.
    """
    try:
        data = json.loads(request.body)
    except (TypeError, ValueError):
        return JsonResponse({"status": "invalid", "message": "Invalid JSON body"}, status=400)

    page = (data.get("page") or "").strip()
    if not page:
        return JsonResponse({"status": "invalid", "message": "'page' wajib diisi"}, status=400)

    try:
        visit_duration = max(0, int(data.get("visit_duration") or 0))
    except (TypeError, ValueError):
        visit_duration = 0

    engagement_score = normalize_engagement_score(data.get("engagement_score"))

    VisitorLog.objects.create(
        page_visited=page,
        visit_duration=visit_duration,
        engagement_score=engagement_score,
        visitor_ip=request.META.get('REMOTE_ADDR'),
    )
    return JsonResponse({"status": "tracked"})


# =====================
# ADMIN PAGES (CUSTOM)
# =====================

def log_admin_activity(message):
    AdminActivity.objects.create(activity=message)


# =====================================================
# AUTO-TAGGING CORE VALUE (Brilian Way)
# =====================================================
def auto_assign_core_values(event):
    """
    Klasifikasi `event` ke satu/lebih CoreValue pakai LLM (Groq) — sama
    seperti client Groq yang dipakai fitur chat AI. Prompt-nya disusun dari
    core_value_name + description yang ada di tabel core_values, jadi kalau
    deskripsi core value diubah lewat admin/DB, klasifikasinya ikut
    menyesuaikan tanpa perlu sentuh kode ini sama sekali.

    Sinkronisasi EventCoreValue-nya sama seperti sebelumnya:
      - core value yang cocok tapi belum terkait -> dibuat
      - core value yang sebelumnya terkait tapi sudah tidak cocok lagi
        (mis. setelah admin edit deskripsi event) -> dihapus

    Dipanggil setiap kali event dibuat (admin_add_event) maupun diedit
    (admin_edit_event). Kalau panggilan ke Groq gagal (API down, quota
    habis, timeout, dsb), fungsi ini diam-diam berhenti tanpa exception,
    supaya proses tambah/edit event tetap berhasil walau tagging-nya
    ter-skip untuk saat itu.
    """
    core_values = list(CoreValue.objects.all())
    if not core_values:
        return

    core_value_list_text = "\n".join(
        f"- {cv.core_value_name}: {cv.description or '-'}" for cv in core_values
    )
    valid_names = {cv.core_value_name.strip().lower(): cv for cv in core_values}

    event_text = f"Judul: {event.event_name or '-'}\nDeskripsi: {event.description or '-'}"

    try:
        client = Groq(api_key=settings.GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Kamu adalah classifier internal perusahaan. Tugasmu "
                        "menentukan core value budaya kerja perusahaan mana saja yang "
                        "paling relevan dengan sebuah event, berdasarkan judul & "
                        "deskripsinya. Daftar core value yang tersedia:\n"
                        f"{core_value_list_text}\n\n"
                        "Balas HANYA dengan nama core value yang cocok, dipisah koma "
                        "(contoh: Integrity, Collaborative). Boleh lebih dari satu, "
                        "boleh juga cuma satu. Kalau tidak ada satupun yang relevan, "
                        "balas persis: NONE. Jangan tambahkan penjelasan apa pun "
                        "selain nama core value (atau NONE)."
                    ),
                },
                {"role": "user", "content": event_text},
            ],
            max_tokens=50,
            temperature=0,
        )
        raw_reply = response.choices[0].message.content.strip()
    except Exception:
        return

    if raw_reply.upper() == "NONE":
        matched_core_values = []
    else:
        matched_core_values = [
            valid_names[name.strip().lower()]
            for name in raw_reply.split(",")
            if name.strip().lower() in valid_names
        ]

    # Hapus relasi lama yang sudah tidak relevan lagi.
    EventCoreValue.objects.filter(event=event).exclude(
        core_value__in=matched_core_values
    ).delete()

    # Tambahkan relasi baru yang belum ada (hindari duplikat).
    existing_ids = set(
        EventCoreValue.objects.filter(event=event)
        .values_list('core_value_id', flat=True)
    )
    EventCoreValue.objects.bulk_create([
        EventCoreValue(event=event, core_value=cv)
        for cv in matched_core_values
        if cv.core_value_id not in existing_ids
    ])


@admin_login_required
def admin_home(request):
    today = timezone.localdate()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.GET.get('action') == 'get_chart_data':
        def format_avg_time(avg_sec):
            if not avg_sec:
                return "0s"
            m = int(avg_sec // 60)
            s = int(avg_sec % 60)
            if m > 0:
                return f"{m}m {s}s"
            return f"{s}s"

        period = request.GET.get('period', '1week')
        labels = []
        data = []
        avg_times = []
        date_range_text = ""

        if period == '1week':
            start_date = today - timedelta(days=6)
            date_range_text = f"{start_date.strftime('%b %d %Y')} - {today.strftime('%b %d %Y')}"
            days = [start_date + timedelta(days=i) for i in range(7)]
            labels = [d.strftime('%d/%m') for d in days]
            qs = VisitorLog.objects.filter(visited_at__date__gte=start_date, visited_at__date__lte=today)
            agg = qs.annotate(period_val=TruncDay('visited_at')).values('period_val').annotate(
                count=Count('log_id'), avg_duration=Avg('visit_duration')
            )
            count_map = {}
            avg_map = {}
            for item in agg:
                if item['period_val']:
                    dt = timezone.localtime(item['period_val']).date() if timezone.is_aware(item['period_val']) else item['period_val'].date()
                    count_map[dt] = count_map.get(dt, 0) + item['count']
                    avg_map[dt] = item['avg_duration']
            for d in days:
                data.append(count_map.get(d, 0))
                avg_times.append(format_avg_time(avg_map.get(d, 0)))

        elif period == '1month':
            start_date = today - timedelta(days=29)
            date_range_text = f"{start_date.strftime('%b %d %Y')} - {today.strftime('%b %d %Y')}"
            days = [start_date + timedelta(days=i) for i in range(30)]
            labels = [d.strftime('%d/%m') for d in days]
            qs = VisitorLog.objects.filter(visited_at__date__gte=start_date, visited_at__date__lte=today)
            agg = qs.annotate(period_val=TruncDay('visited_at')).values('period_val').annotate(
                count=Count('log_id'), avg_duration=Avg('visit_duration')
            )
            count_map = {}
            avg_map = {}
            for item in agg:
                if item['period_val']:
                    dt = timezone.localtime(item['period_val']).date() if timezone.is_aware(item['period_val']) else item['period_val'].date()
                    count_map[dt] = count_map.get(dt, 0) + item['count']
                    avg_map[dt] = item['avg_duration']
            for d in days:
                data.append(count_map.get(d, 0))
                avg_times.append(format_avg_time(avg_map.get(d, 0)))

        elif period in ['6months', '1year']:
            months_count = 6 if period == '6months' else 12
            start_date = today.replace(day=1)
            for _ in range(months_count - 1):
                start_date = (start_date - timedelta(days=1)).replace(day=1)
            date_range_text = f"{start_date.strftime('%b %Y')} - {today.strftime('%b %Y')}"
            months_list = []
            curr = start_date
            for _ in range(months_count):
                months_list.append((curr.year, curr.month))
                curr = (curr.replace(day=28) + timedelta(days=4)).replace(day=1)
            labels = [date(m[0], m[1], 1).strftime('%b') for m in months_list]
            qs = VisitorLog.objects.filter(visited_at__date__gte=start_date, visited_at__date__lte=today)
            agg = qs.annotate(period_val=TruncMonth('visited_at')).values('period_val').annotate(
                count=Count('log_id'), avg_duration=Avg('visit_duration')
            )
            count_map = {}
            avg_map = {}
            for item in agg:
                if item['period_val']:
                    dt = timezone.localtime(item['period_val']).date() if timezone.is_aware(item['period_val']) else item['period_val'].date()
                    count_map[(dt.year, dt.month)] = count_map.get((dt.year, dt.month), 0) + item['count']
                    avg_map[(dt.year, dt.month)] = item['avg_duration']
            for m in months_list:
                data.append(count_map.get(m, 0))
                avg_times.append(format_avg_time(avg_map.get(m, 0)))

        return JsonResponse({
            'labels': labels, 'data': data,
            'avg_times': avg_times, 'date_range_text': date_range_text
        })

    start_date_initial = today - timedelta(days=6)
    initial_date_range_text = f"{start_date_initial.strftime('%b %d %Y')} - {today.strftime('%b %d %Y')}"
    days = [start_date_initial + timedelta(days=i) for i in range(7)]
    weekly_labels = [d.strftime('%d/%m') for d in days]

    def format_avg_time(avg_sec):
        if not avg_sec:
            return "0s"
        m = int(avg_sec // 60)
        s = int(avg_sec % 60)
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    qs_initial = VisitorLog.objects.filter(visited_at__date__gte=start_date_initial, visited_at__date__lte=today)
    agg_initial = qs_initial.annotate(period_val=TruncDay('visited_at')).values('period_val').annotate(
        count=Count('log_id'), avg_duration=Avg('visit_duration')
    )
    count_map_initial = {}
    avg_map_initial = {}
    for item in agg_initial:
        if item['period_val']:
            dt = timezone.localtime(item['period_val']).date() if timezone.is_aware(item['period_val']) else item['period_val'].date()
            count_map_initial[dt] = count_map_initial.get(dt, 0) + item['count']
            avg_map_initial[dt] = item['avg_duration']
    weekly_data = []
    weekly_avg_times = []
    for d in days:
        weekly_data.append(count_map_initial.get(d, 0))
        weekly_avg_times.append(format_avg_time(avg_map_initial.get(d, 0)))

    total_visits = VisitorLog.objects.count()
    avg_visit_seconds = VisitorLog.objects.aggregate(avg_duration=Avg('visit_duration'))['avg_duration'] or 0
    avg_time = f"{int(avg_visit_seconds // 60)}m {int(avg_visit_seconds % 60)}s"
    avg_engagement_score = normalize_engagement_score(
        VisitorLog.objects.aggregate(avg_engagement=Avg('engagement_score'))['avg_engagement'] or 0
    )
    if avg_engagement_score >= 70:
        engagement_text = 'High'
    elif avg_engagement_score >= 40:
        engagement_text = 'Medium'
    else:
        engagement_text = 'Low'

    engagement_percentage = f"{avg_engagement_score}%"

    last_week_visits = VisitorLog.objects.filter(visited_at__date__gte=start_date_initial, visited_at__date__lte=today).count()
    previous_week_visits = VisitorLog.objects.filter(
        visited_at__date__gte=start_date_initial - timedelta(days=7),
        visited_at__date__lt=start_date_initial
    ).count()
    if previous_week_visits:
        trends = f"{round((last_week_visits - previous_week_visits) / previous_week_visits * 100)}%"
    else:
        trends = f"+{last_week_visits * 10}%" if last_week_visits else "0%"

    total_content = Event.objects.count() + News.objects.count() + Attribute.objects.count()
    total_views = VisitorLog.objects.count()
    active_content = Event.objects.filter(status__in=[Event.STATUS_PUBLISHED, Event.STATUS_ONGOING]).count()

    # Only feedback from participants who actually attended the event should
    # count toward the dashboard's totals / pie chart / recent list — same
    # rule already used by feedback_admin(). "Attended" = they have an
    # Attendance record tied to their event registration for that same event
    # (matched by event + email). Feedback with no event ('General') can
    # never satisfy this, since attendance is always tied to a specific event.
    # Using this single queryset everywhere below (instead of each stat
    # re-filtering Feedback on its own with just is_genuine_feedback=True) is
    # what keeps the stat cards, the pie chart, and Recent Feedback in sync.
    dashboard_attended_qs = Attendance.objects.filter(
        event_registration__event_id=OuterRef('event_id'),
        event_registration__email__iexact=OuterRef('participant_email'),
    )
    genuine_attended_feedback_qs = Feedback.objects.filter(
        is_genuine_feedback=True,
    ).annotate(
        has_attended=Exists(dashboard_attended_qs)
    ).filter(has_attended=True)

    total_feedback = genuine_attended_feedback_qs.count()

    feedback_messages = [item.message for item in genuine_attended_feedback_qs]

    event_ranking = []
    event_feedback_stats = (
        genuine_attended_feedback_qs
        .filter(event__isnull=False)
        .values('event__event_id', 'event__event_name')
        .annotate(
            total=Count('feedback_id'),
            positive=Count('feedback_id', filter=Q(sentiment='positive')),
            negative=Count('feedback_id', filter=Q(sentiment='negative')),
        )
        .order_by('-total')
    )

    for row in event_feedback_stats:
        if row['total'] == 0:
            continue
        score = round(row['positive'] / row['total'] * 100) if row['total'] else 0
        event_ranking.append({
            'event_id': row['event__event_id'],
            'event': row['event__event_name'] or 'Event tidak bernama',
            'total': row['total'],
            'positive': row['positive'],
            'negative': row['negative'],
            'score': score,
        })

    event_ranking.sort(key=lambda row: (row['score'], row['total']), reverse=True)
    event_ranking = event_ranking[:5]

    total_positive = genuine_attended_feedback_qs.filter(sentiment='positive').count()
    total_neutral = genuine_attended_feedback_qs.filter(sentiment='neutral').count()
    total_negative = genuine_attended_feedback_qs.filter(sentiment='negative').count()

    if total_feedback:
        positive_percent = round(total_positive / total_feedback * 100)
        neutral_percent = round(total_neutral / total_feedback * 100)
        negative_percent = round(total_negative / total_feedback * 100)
    else:
        positive_percent = neutral_percent = negative_percent = 0

    recent_admin_activities = list(AdminActivity.objects.all().order_by('-created_at'))
    recent_feedbacks = list(genuine_attended_feedback_qs.order_by('-created_at')[:10])

    def format_activity_time(dt):
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        now = timezone.localtime(timezone.now())
        dt = timezone.localtime(dt)
        diff = now - dt
        seconds = diff.seconds
        if diff.days == 0:
            if seconds < 60:
                return f"{seconds}s ago"
            if seconds < 3600:
                return f"{seconds // 60}m ago"
            return f"Today {dt.strftime('%H:%M')}"
        if diff.days == 1:
            return f"Yesterday {dt.strftime('%H:%M')}"
        if diff.days < 7:
            return f"{diff.days} days ago"
        return dt.strftime('%d %b %Y %H:%M')

    recent_admin_activities_data = []
    for activity in recent_admin_activities:
        recent_admin_activities_data.append({
            'icon': '📝',
            'title': activity.activity,
            'when': format_activity_time(activity.created_at),
        })

    recent_feedbacks_data = []
    for fb in recent_feedbacks:
        preview = fb.message.strip().replace('\n', ' ')
        if len(preview) > 90:
            preview = preview[:90].rsplit(' ', 1)[0] + '...'
        recent_feedbacks_data.append({
            'comment': preview,
            'sentiment': fb.sentiment.capitalize() if fb.sentiment else 'Neutral',
            'rating': fb.rating if fb.rating is not None else 'N/A',
            'platform': fb.source_platform.capitalize(),
            'when': format_activity_time(fb.created_at),
        })

    context = {
        'total_content': total_content,
        'total_views': total_views,
        'active_content': active_content,
        'total_feedback': total_feedback,
        'weekly_data': weekly_data,
        'weekly_avg_times_json': json.dumps(weekly_avg_times),
        'weekly_labels': weekly_labels,
        'weekly_data_json': json.dumps(weekly_data),
        'weekly_labels_json': json.dumps(weekly_labels),
        'initial_date_range_text': initial_date_range_text,
        'recent_admin_activities': recent_admin_activities_data,
        'recent_feedbacks': recent_feedbacks_data,
        'total_visits': total_visits,
        'avg_time': avg_time,
        'engagement_text': engagement_text,
        'engagement_percentage': engagement_percentage,
        'trends': trends,
        'positive': total_positive,
        'neutral': total_neutral,
        'negative': total_negative,
        'positive_percent': positive_percent,
        'neutral_percent': neutral_percent,
        'negative_percent': negative_percent,
        'event_ranking': event_ranking,
    }
    return render(request, 'cms/admin.html', context)


@admin_login_required
def explore_admin(request):
    # Admin sees ALL events regardless of status
    events = list(Event.objects.all().order_by('-event_date'))
    news_items = list(News.objects.all().order_by('-created_at'))
    attributes = list(Attribute.objects.all().order_by('-created_at'))
    today = date.today()

    # ── Batched view counts / last-viewed timestamps ──────────────
    # One aggregate query per content type instead of 2 queries per item.
    view_stats = {
        row['page_visited']: {'views': row['views'], 'last_viewed': row['last_viewed']}
        for row in VisitorLog.objects.filter(
            page_visited__in=(
                [f'event_detail/{e.event_id}' for e in events]
                + [f'news_detail/{n.news_id}' for n in news_items]
                + [f'attribute_detail/{a.attribute_id}' for a in attributes]
            )
        ).values('page_visited').annotate(
            views=Count('log_id'),
            last_viewed=Max('visited_at'),
        )
    }

    # ── Batched registrant counts (one GROUP BY instead of N queries) ──
    registrant_counts = {
        row['event_id']: row['count']
        for row in EventRegistration.objects.values('event_id').annotate(count=Count('registration_id'))
    }

    for event in events:
        stats = view_stats.get(f'event_detail/{event.event_id}', {})
        event.views = stats.get('views', 0)
        event.registrant_count = registrant_counts.get(event.event_id, 0)

    for n in news_items:
        stats = view_stats.get(f'news_detail/{n.news_id}', {})
        n.views = stats.get('views', 0)
        n.last_viewed = stats.get('last_viewed')

    for a in attributes:
        stats = view_stats.get(f'attribute_detail/{a.attribute_id}', {})
        a.views = stats.get('views', 0)
        a.last_viewed = stats.get('last_viewed')

    context = {
        'events': events,
        'news_items': news_items,
        'attributes': attributes,
        'status_choices': Event.STATUS_CHOICES,
    }
    return render(request, 'cms/explore_admin.html', context)


def redirect_news(request, id):
    news_item = get_object_or_404(News, news_id=id)
    VisitorLog.objects.create(
        page_visited=f'news_detail/{id}',
        visitor_ip=request.META.get('REMOTE_ADDR')
    )
    if news_item.image_file:
        return redirect(news_item.image_file.url)
    return redirect(news_item.image_url if news_item.image_url else '/explore/')


def redirect_attribute(request, id):
    attr = get_object_or_404(Attribute, attribute_id=id)
    VisitorLog.objects.create(
        page_visited=f'attribute_detail/{id}',
        visitor_ip=request.META.get('REMOTE_ADDR')
    )
    return redirect(attr.file_url if attr.file_url else '/explore/')


@admin_login_required
def admin_add_event(request):
    if request.method == 'POST':
        event_name  = request.POST.get('event_name', '').strip()
        description = request.POST.get('description', '').strip()
        location    = request.POST.get('location', '').strip()
        event_date  = request.POST.get('event_date')
        event_time  = request.POST.get('event_time')
        requested_status = request.POST.get('status', Event.STATUS_DRAFT)
        # ongoing/completed are computed automatically by auto_update_event_statuses();
        # admin CRUD may only request draft / published / archived.
        if requested_status not in (Event.STATUS_DRAFT, Event.STATUS_PUBLISHED, Event.STATUS_ARCHIVED):
            requested_status = Event.STATUS_DRAFT
        capacity    = request.POST.get('capacity') or None
        person_in_charge       = request.POST.get('person_in_charge', '').strip() or None
        registration_deadline  = request.POST.get('registration_deadline') or None
        attendance_open_time   = request.POST.get('attendance_open_time') or None
        attendance_close_time  = request.POST.get('attendance_close_time') or None

        image_file = request.FILES.get('image')
        image_path = None
        if image_file:
            media_dir = os.path.join(settings.MEDIA_ROOT or 'media', 'events')
            os.makedirs(media_dir, exist_ok=True)
            fs = FileSystemStorage(location=media_dir)
            filename = fs.save(image_file.name, image_file)
            image_path = f'events/{filename}'

        errors = []
        parsed_event_date = None
        if event_date:
            try:
                parsed_event_date = date.fromisoformat(event_date)
            except (TypeError, ValueError):
                errors.append("Format 'Tanggal Event' tidak valid.")

        parsed_registration_deadline = _parse_admin_datetime(registration_deadline, 'Registration Deadline', errors)
        parsed_attendance_open_time = _parse_admin_datetime(attendance_open_time, 'Attendance Open Time', errors)
        parsed_attendance_close_time = _parse_admin_datetime(attendance_close_time, 'Attendance Close Time', errors)

        if capacity:
            try:
                capacity = int(capacity)
            except (TypeError, ValueError):
                errors.append("'Capacity' harus berupa angka.")
                capacity = None

        if not event_name:
            errors.append("'Nama Event' wajib diisi.")

        end_time = request.POST.get('end_time')
        if event_time and end_time:
            try:
                if time.fromisoformat(end_time) <= time.fromisoformat(event_time):
                    errors.append("'Waktu Selesai' harus setelah 'Waktu Mulai'.")
            except (TypeError, ValueError):
                errors.append("Format waktu event tidak valid.")

        # Lifecycle gate: Published is only honored when every required
        # field (Capacity & PIC excluded — they're always optional) is
        # filled in. If the admin explicitly asked to Publish but something
        # required is still missing, this is a hard validation error — the
        # event is NOT saved, and the admin is told exactly what to fill in
        # before it can be published. Leaving status as Draft never
        # triggers this check.
        if requested_status == Event.STATUS_PUBLISHED:
            missing_fields = get_missing_required_fields_for_publish({
                'event_name': event_name,
                'description': description,
                'location': location,
                'event_date': parsed_event_date,
                'event_time': event_time or None,
                'end_time': end_time or None,
                'image_url': image_path,
                'registration_deadline': parsed_registration_deadline,
                'attendance_open_time': parsed_attendance_open_time,
                'attendance_close_time': parsed_attendance_close_time,
            })
            if missing_fields:
                errors.append(
                    "Event tidak bisa dipublish. Lengkapi field wajib berikut terlebih dahulu: "
                    + ", ".join(missing_fields) + "."
                )

        if not errors:
            new_event = Event.objects.create(
                event_name=event_name,
                description=description or None,
                location=location or None,
                event_date=parsed_event_date,
                event_time=event_time or None,
                end_time=end_time or None,
                image_url=image_path,
                status=requested_status,
                capacity=capacity or None,
                person_in_charge=person_in_charge,
                registration_deadline=parsed_registration_deadline,
                attendance_open_time=parsed_attendance_open_time,
                attendance_close_time=parsed_attendance_close_time,
            )
            auto_assign_core_values(new_event)
            log_admin_activity(f"Event '{event_name}' dibuat dengan status '{requested_status}'")
            return redirect('admin_explore')

        # Validasi gagal (mis. Publish tapi field wajib belum lengkap) ->
        # event TIDAK disimpan ke DB (sengaja, sesuai aturan bisnis). Supaya
        # form TIDAK terlihat "reset" ke admin, bangun objek Event sementara
        # (belum di-save ke database) dari input yang barusan diketik, dan
        # kirim itu sebagai `event` ke template — template lalu membaca
        # ulang value setiap field dari objek ini persis seperti saat Edit
        # Event, sehingga semua isian sebelumnya tetap tampil.
        temp_event = Event(
            event_name=event_name,
            description=description,
            location=location,
            event_date=parsed_event_date,
            event_time=event_time or None,
            end_time=end_time or None,
            image_url=image_path,
            status=requested_status,
            capacity=capacity or None,
            person_in_charge=person_in_charge,
            registration_deadline=parsed_registration_deadline,
            attendance_open_time=parsed_attendance_open_time,
            attendance_close_time=parsed_attendance_close_time,
        )

        return render(request, 'cms/event_form.html', {
            'action': 'Tambah Event',
            'event': temp_event,
            'is_edit': False,
            'form_action': 'admin_add_event',
            'status_choices': Event.STATUS_CHOICES,
            'errors': errors,
        })

    return render(request, 'cms/event_form.html', {
        'action': 'Tambah Event',
        'event': None,
        'is_edit': False,
        'form_action': 'admin_add_event',
        'status_choices': Event.STATUS_CHOICES,
    })


@admin_login_required
def admin_edit_event(request, id):
    event = get_object_or_404(Event, event_id=id)
    if request.method == 'POST':
        old_name = event.event_name
        event.event_name   = request.POST.get('event_name', '').strip() or event.event_name
        event.description  = request.POST.get('description', '').strip() or event.description
        event.location     = request.POST.get('location', '').strip() or event.location
        # ongoing/completed are computed automatically by auto_update_event_statuses();
        # admin CRUD may only request draft / published / archived. The
        # actual status saved is decided AFTER all fields below are applied
        # to `event` (see the lifecycle gate near the end of this view) —
        # Published is only honored once every required field is complete.
        requested_status = request.POST.get('status', event.status)
        if requested_status not in (Event.STATUS_DRAFT, Event.STATUS_PUBLISHED, Event.STATUS_ARCHIVED):
            requested_status = Event.STATUS_DRAFT
        event.person_in_charge = request.POST.get('person_in_charge', '').strip() or event.person_in_charge

        errors = []

        capacity = request.POST.get('capacity')
        if capacity:
            try:
                event.capacity = int(capacity)
            except (TypeError, ValueError):
                errors.append("'Capacity' harus berupa angka.")

        reg_dl = request.POST.get('registration_deadline')
        att_open = request.POST.get('attendance_open_time')
        att_close = request.POST.get('attendance_close_time')
        if reg_dl:
            parsed = _parse_admin_datetime(reg_dl, 'Registration Deadline', errors)
            if parsed is not None:
                event.registration_deadline = parsed
        if att_open:
            parsed = _parse_admin_datetime(att_open, 'Attendance Open Time', errors)
            if parsed is not None:
                event.attendance_open_time = parsed
        if att_close:
            parsed = _parse_admin_datetime(att_close, 'Attendance Close Time', errors)
            if parsed is not None:
                event.attendance_close_time = parsed

        event_date = request.POST.get('event_date')
        event_time = request.POST.get('event_time')
        end_time   = request.POST.get('end_time')

        if event_date:
            try:
                event.event_date = date.fromisoformat(event_date)
            except (TypeError, ValueError):
                errors.append("Format 'Tanggal Event' tidak valid.")

        if event_time and end_time:
            try:
                if time.fromisoformat(end_time) <= time.fromisoformat(event_time):
                    errors.append("'Waktu Selesai' harus setelah 'Waktu Mulai'.")
            except (TypeError, ValueError):
                errors.append("Format waktu event tidak valid.")

        if errors:
            return render(request, 'cms/event_form.html', {
                'action': 'Edit Event',
                'event': event,
                'is_edit': True,
                'form_action': 'admin_edit_event',
                'status_choices': Event.STATUS_CHOICES,
                'errors': errors,
            })

        image_file = request.FILES.get('image')
        if image_file:
            media_dir = os.path.join(settings.MEDIA_ROOT or 'media', 'events')
            os.makedirs(media_dir, exist_ok=True)
            fs = FileSystemStorage(location=media_dir)
            filename = fs.save(image_file.name, image_file)
            event.image_url = f'events/{filename}'

        event.event_time = event_time or event.event_time
        event.end_time   = end_time or event.end_time

        # Lifecycle gate — checked last, once every other field on `event`
        # (including a freshly-uploaded image, if any) reflects this save,
        # so it's evaluated against the actual post-edit state. Published
        # is only honored when every required field (Capacity & PIC
        # excluded — always optional) is complete; if the admin explicitly
        # asked to Publish but something is missing, this is a hard
        # validation error — nothing is saved, and the admin is told
        # exactly what to fill in. This is also what makes "Completed →
        # re-save as Published" work: there's no special-case for the
        # previous status here, a Completed event goes through the exact
        # same completeness check as any other.
        if requested_status == Event.STATUS_PUBLISHED:
            missing_fields = event.get_missing_required_fields()
            if missing_fields:
                return render(request, 'cms/event_form.html', {
                    'action': 'Edit Event',
                    'event': event,
                    'is_edit': True,
                    'form_action': 'admin_edit_event',
                    'status_choices': Event.STATUS_CHOICES,
                    'errors': [
                        "Event tidak bisa dipublish. Lengkapi field wajib berikut terlebih dahulu: "
                        + ", ".join(missing_fields) + "."
                    ],
                })

        event.status = requested_status
        event.save()
        auto_assign_core_values(event)
        log_admin_activity(f"Event '{old_name}' diperbarui → '{event.event_name}' (status: {event.status})")
        return redirect('admin_explore')

    return render(request, 'cms/event_form.html', {
        'action': 'Edit Event',
        'event': event,
        'is_edit': True,
        'form_action': 'admin_edit_event',
        'status_choices': Event.STATUS_CHOICES,
    })


@admin_login_required
def admin_delete_event(request, id):
    if request.method == 'POST':
        event = get_object_or_404(Event, event_id=id)
        event_name = event.event_name
        try:
            event.delete()
            log_admin_activity(f"Event '{event_name}' dihapus")
        except ProtectedError:
            messages.error(
                request,
                f"Event '{event_name}' tidak bisa dihapus karena sudah punya peserta terdaftar. "
                f"Gunakan status 'Archived' untuk menonaktifkannya tanpa menghapus data peserta."
            )
    return redirect('admin_explore')


@admin_login_required
def admin_event_preview(request, id):
    event = get_object_or_404(Event, event_id=id)
    context = {'event': event, 'today': date.today()}
    return render(request, 'cms/admin_event_preview.html', context)


# ── Change status via AJAX / POST ───────────────────────────────
@admin_login_required
def admin_change_event_status(request, id):
    if request.method == 'POST':
        event = get_object_or_404(Event, event_id=id)
        new_status = request.POST.get('status') or json.loads(request.body or '{}').get('status')
        # ongoing/completed are automation-only (see auto_update_event_statuses());
        # this manual endpoint may only request draft / published / archived.
        valid = [Event.STATUS_DRAFT, Event.STATUS_PUBLISHED, Event.STATUS_ARCHIVED]
        if new_status in valid:
            # Same lifecycle gate as admin_add_event/admin_edit_event: a
            # request to Publish only succeeds if the event is actually
            # complete (Capacity & PIC excluded — always optional).
            # Otherwise nothing is changed — the admin is required to fill
            # in the missing fields (via Edit Event) before this event can
            # be published.
            if new_status == Event.STATUS_PUBLISHED and not event.is_ready_to_publish:
                missing = ", ".join(event.get_missing_required_fields())
                message = (
                    f"Event '{event.event_name}' belum bisa dipublish. "
                    f"Lengkapi field wajib berikut terlebih dahulu: {missing}."
                )
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'status': 'error', 'message': message}, status=400)
                messages.error(request, message)
                return redirect('admin_explore')

            old = event.status
            event.status = new_status
            event.save()
            log_admin_activity(f"Status event '{event.event_name}' diubah dari '{old}' ke '{new_status}'")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'success', 'new_status': new_status})
            return redirect('admin_explore')
        return JsonResponse({'status': 'error', 'message': 'Status tidak valid'}, status=400)
    return JsonResponse({'status': 'error'}, status=405)


@admin_login_required
def admin_add_news(request):
    if request.method == 'POST':
        title      = request.POST.get('title', '').strip()
        edition_id = request.POST.get('edition')
        content    = request.POST.get('content', '').strip()
        image_file = request.FILES.get('image_file')
        edition = NewsEdition.objects.filter(edition_id=edition_id).first() if edition_id else None
        if title:
            News.objects.create(
                title=title, edition=edition,
                content=content or None, image_file=image_file or None
            )
            log_admin_activity(f"News '{title}' dibuat")
            return redirect('admin_explore')
    editions = NewsEdition.objects.all()
    return render(request, 'cms/news_form.html', {
        'action': 'Tambah News', 'news': None,
        'editions': editions, 'form_action': 'admin_add_news',
    })


@admin_login_required
def admin_edit_news(request, id):
    news_item = get_object_or_404(News, news_id=id)
    if request.method == 'POST':
        old_title = news_item.title
        news_item.title = request.POST.get('title', '').strip() or news_item.title
        edition_id = request.POST.get('edition')
        if edition_id:
            news_item.edition = NewsEdition.objects.filter(edition_id=edition_id).first()
        news_item.content = request.POST.get('content', '').strip() or news_item.content
        image_file = request.FILES.get('image_file')
        if image_file:
            news_item.image_file = image_file
        news_item.save()
        log_admin_activity(f"News '{old_title}' diperbarui menjadi '{news_item.title}'")
        return redirect('admin_explore')
    editions = NewsEdition.objects.all()
    return render(request, 'cms/news_form.html', {
        'action': 'Edit News', 'news': news_item,
        'editions': editions, 'form_action': 'admin_edit_news',
    })


@admin_login_required
def admin_delete_news(request, id):
    if request.method == 'POST':
        news_item = get_object_or_404(News, news_id=id)
        title = news_item.title
        news_item.delete()
        log_admin_activity(f"News '{title}' dihapus")
    return redirect('admin_explore')


@admin_login_required
def admin_add_attribute(request):
    if request.method == 'POST':
        attribute_name = request.POST.get('attribute_name', '').strip()
        attribute_type = request.POST.get('attribute_type', '').strip()
        file_url = request.POST.get('file_url', '').strip()
        if attribute_name:
            Attribute.objects.create(
                attribute_name=attribute_name,
                attribute_type=attribute_type or None,
                file_url=file_url or None
            )
            log_admin_activity(f"Attribute '{attribute_name}' dibuat")
            return redirect('admin_explore')
    from .models import ATTRIBUTE_TYPES
    return render(request, 'cms/attribute_form.html', {
        'action': 'Tambah Attribute', 'attribute': None,
        'attribute_types': ATTRIBUTE_TYPES, 'form_action': 'admin_add_attribute',
    })


@admin_login_required
def admin_edit_attribute(request, id):
    attribute = get_object_or_404(Attribute, attribute_id=id)
    if request.method == 'POST':
        old_name = attribute.attribute_name
        attribute.attribute_name = request.POST.get('attribute_name', '').strip() or attribute.attribute_name
        attribute.attribute_type = request.POST.get('attribute_type', '').strip() or attribute.attribute_type
        attribute.file_url = request.POST.get('file_url', '').strip() or attribute.file_url
        attribute.save()
        log_admin_activity(f"Attribute '{old_name}' diperbarui menjadi '{attribute.attribute_name}'")
        return redirect('admin_explore')
    from .models import ATTRIBUTE_TYPES
    return render(request, 'cms/attribute_form.html', {
        'action': 'Edit Attribute', 'attribute': attribute,
        'attribute_types': ATTRIBUTE_TYPES, 'form_action': 'admin_edit_attribute',
    })


@admin_login_required
def admin_delete_attribute(request, id):
    if request.method == 'POST':
        attribute = get_object_or_404(Attribute, attribute_id=id)
        name = attribute.attribute_name
        attribute.delete()
        log_admin_activity(f"Attribute '{name}' dihapus")
    return redirect('admin_explore')


@admin_login_required
def feedback_admin(request):
    selected_events = request.GET.getlist('event')
    if not selected_events:
        selected_events = ['all']
    selected_sentiment = request.GET.get('sentiment', 'all')
    search_query = request.GET.get('search', '').strip()
    page_number = request.GET.get('page', 1)
    # Use event FK filtering. Provide event options as dicts with id/name for template.
    event_qs = Event.objects.filter(status__in=['completed', 'archived']).order_by('event_name')
    event_options = list(event_qs.values('event_id', 'event_name'))

    # Only feedback from participants who actually attended the event should count
    # toward the totals / pie chart / recent list. "Attended" = they have an
    # Attendance record tied to their event registration for that same event
    # (matched by event + email). Feedback with no event ('General') can never
    # satisfy this, since attendance is always tied to a specific event.
    attended_qs = Attendance.objects.filter(
        event_registration__event_id=OuterRef('event_id'),
        event_registration__email__iexact=OuterRef('participant_email'),
    )

    base_qs = Feedback.objects.filter(
        is_genuine_feedback=True,
    ).annotate(
        has_attended=Exists(attended_qs)
    ).filter(has_attended=True).order_by('-created_at')

    # Filter by selected event(s) using FK. 'General' means event is null.
    if selected_events and 'all' not in selected_events:
        q_filter = Q()
        for evt in selected_events:
            if evt == 'General':
                q_filter |= Q(event__isnull=True)
            else:
                try:
                    ev_id = int(evt)
                    q_filter |= Q(event__event_id=ev_id)
                except Exception:
                    # ignore invalid values
                    continue
        base_qs = base_qs.filter(q_filter)

    # Separate `if` (not `elif`) so event filter and text search can combine —
    # admin should be able to filter by event AND search text at the same time.
    if search_query:
        base_qs = base_qs.filter(message__icontains=search_query)

    all_feedbacks = list(base_qs)
    total_positive = total_neutral = total_negative = 0
    feedback_entries = []

    for fb in all_feedbacks:
        # Always get a real confidence score from the model — fb.sentiment
        # (when present) stays the authoritative label since it's the value
        # recalculated with rating in save_feedback() and used consistently
        # elsewhere (culture_performance, etc), but the confidence % shown
        # here must come from the model, not a fabricated 100%.
        try:
            predicted_sentiment, confidence = sentiment_analyzer.predict(fb.message, fb.rating)
        except Exception:
            predicted_sentiment, confidence = 'neutral', 0.50

        pred_sentiment = fb.sentiment or predicted_sentiment

        if pred_sentiment == 'positive':
            total_positive += 1
        elif pred_sentiment == 'negative':
            total_negative += 1
        else:
            total_neutral += 1

        if selected_sentiment == 'all' or selected_sentiment == pred_sentiment:
            sentiment_text = f"{pred_sentiment.capitalize()} ({round(confidence * 100)}%)"
            feedback_entries.append({
                'comment': fb.message,
                'time': fb.created_at.strftime('%d %b %Y %H:%M'),
                'user': fb.session_id or 'Anonymous',
                'event': fb.event.event_name if fb.event else 'General',
                'sentiment': sentiment_text,
                'rating': fb.rating if fb.rating is not None else 'N/A',
                'platform': fb.source_platform.capitalize(),
                'confidence': confidence,
                'original_obj': fb
            })

    total_feedback = total_positive + total_neutral + total_negative
    if total_feedback:
        positive_percent = round(total_positive / total_feedback * 100)
        neutral_percent  = round(total_neutral  / total_feedback * 100)
        negative_percent = round(total_negative / total_feedback * 100)
    else:
        positive_percent = neutral_percent = negative_percent = 0

    paginator = Paginator(feedback_entries, 10)
    page_obj = paginator.get_page(page_number)

    most_common = sentiment_analyzer.extract_common_words(
        [fb.message for fb in base_qs], limit=8
    )
    most_common_words = [{'word': word, 'count': count} for word, count in most_common]

    context = {
        'feedbacks': page_obj,
        'page_obj': paginator.get_page(page_number),
        'selected_events': selected_events,
        'selected_event': selected_events[0] if selected_events else 'all',
        'selected_sentiment': selected_sentiment,
        'search_query': search_query,
        'positive': total_positive,
        'neutral': total_neutral,
        'negative': total_negative,
        'positive_percent': positive_percent,
        'neutral_percent': neutral_percent,
        'negative_percent': negative_percent,
        'event_options': event_options,
        'most_common_words': most_common_words,
    }
    return render(request, 'cms/feedback_admin.html', context)


# =====================
# ATTENDANCE VIEWS
# =====================
from .models import Attendance


def submit_attendance(request, id):
    """Participant self-attendance form (GPS + photo)."""
    event = get_object_or_404(Event, event_id=id)

    # Business rule: attendance only when window is open
    if not event.is_attendance_open:
        return render(request, 'cms/attendance_form.html', {
            'event': event,
            'errors': ['Waktu absensi untuk event ini belum dibuka atau sudah ditutup.'],
        })

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        latitude  = request.POST.get('latitude', '').strip()
        longitude = request.POST.get('longitude', '').strip()
        photo     = request.FILES.get('photo_evidence')

        errors = []
        if not email:
            errors.append('Email wajib diisi.')
        if not latitude or not longitude:
            errors.append('Lokasi GPS wajib diambil sebelum mengirim.')
        if not photo:
            errors.append('Foto bukti kehadiran wajib diunggah.')

        # Check registration exists
        reg = EventRegistration.objects.filter(
            event_id=event.event_id,
            email=email,
        ).first()
        if not errors and not reg:
            errors.append('Email ini tidak terdaftar pada event ini.')

        # Check duplicate attendance
        if not errors and reg:
            already = Attendance.objects.filter(
                event_registration=reg,
                status__in=[Attendance.STATUS_PENDING, Attendance.STATUS_VERIFIED],
            ).exists()
            if already:
                errors.append('Anda sudah melakukan attendance untuk event ini.')

        if errors:
            return render(request, 'cms/attendance_form.html', {
                'event': event, 'errors': errors,
            })

        Attendance.objects.create(
            event_registration=reg,
            participant_email=email,
            photo_evidence=photo,
            latitude=latitude or None,
            longitude=longitude or None,
        )

        # Simpan email ke session supaya status partisipasi di Event Detail
        # ikut terupdate begitu peserta kembali ke halaman tersebut.
        request.session['participant_email'] = email

        return render(request, 'cms/attendance_form.html', {
            'event': event,
            'show_success': True,
            'event_name': event.event_name,
        })

    return render(request, 'cms/attendance_form.html', {'event': event})


def _resolve_period_range(period, year, quarter, month):
    """
    Terjemahkan parameter filter (period + year/quarter/month) dari
    query string menjadi (start_datetime, end_datetime, trunc_function,
    label) yang dipakai untuk memotong queryset berdasarkan tanggal dan
    mengelompokkan hasil agregasi. Dipakai oleh bagian Attendance
    Analytics (Step 8) di halaman gabungan ini.

    period: 'monthly' | 'quarterly' | 'ytd'
    """
    now = timezone.now()
    year = int(year) if year else now.year

    if period == 'quarterly':
        quarter = int(quarter) if quarter else ((now.month - 1) // 3) + 1
        start_month = (quarter - 1) * 3 + 1
        start = datetime(year, start_month, 1, tzinfo=now.tzinfo)
        end_month = start_month + 2
        if end_month == 12:
            end = datetime(year, 12, 31, 23, 59, 59, tzinfo=now.tzinfo)
        else:
            end = datetime(year, end_month + 1, 1, tzinfo=now.tzinfo) - timedelta(seconds=1)
        trunc = TruncMonth
        label = f"Q{quarter} {year}"

    elif period == 'ytd':
        start = datetime(year, 1, 1, tzinfo=now.tzinfo)
        end = now if year == now.year else datetime(year, 12, 31, 23, 59, 59, tzinfo=now.tzinfo)
        trunc = TruncMonth
        label = f"YTD {year}"

    else:  # 'monthly' (default)
        month = int(month) if month else now.month
        start = datetime(year, month, 1, tzinfo=now.tzinfo)
        if month == 12:
            end = datetime(year, 12, 31, 23, 59, 59, tzinfo=now.tzinfo)
        else:
            end = datetime(year, month + 1, 1, tzinfo=now.tzinfo) - timedelta(seconds=1)
        trunc = TruncDay
        label = start.strftime('%B %Y')

    return start, end, trunc, label


@admin_login_required
def admin_attendance_list(request):
    """
    Halaman gabungan "Attendance" di admin — digabung jadi satu page:
      1) Attendance Verification (tabel + aksi verify/reject — sudah ada)
      2) Attendance Analytics (Step 8 — KPI + chart, filter periode)
      3) Participation Analytics (Step 9 — growth peserta, filter granularitas)

    Semua query string filter dipisah prefix-nya agar tidak bertabrakan:
      - status            -> filter tabel verifikasi
      - event             -> filter tabel verifikasi berdasarkan nama event
      - period/year/quarter/month       -> filter Attendance Analytics
      - granularity/p_year              -> filter Participation Analytics
    """
    # ---------- 1) ATTENDANCE VERIFICATION (tabel) ----------
    current_status = request.GET.get('status', '').strip()
    current_event = request.GET.get('event', '').strip()

    registrations = EventRegistration.objects.prefetch_related('attendances').order_by('-registered_at')
    if current_status:
        if current_status == 'not_attended':
            registrations = registrations.filter(attendances__isnull=True)
        else:
            registrations = registrations.filter(attendances__status=current_status)

    if current_event:
        # Filter pakai `event_name` (bukan FK `event`) karena cukup banyak
        # registrasi lama tidak punya `event` FK terisi (NULL) dan hanya
        # tersambung ke event lewat teks nama — filter di sini tetap harus
        # bisa menjangkau data lama itu, bukan cuma yang FK-nya lengkap.
        registrations = registrations.filter(event_name=current_event)

    attendances_data = []
    for reg in registrations:
        att = reg.attendances.order_by('-attendance_timestamp').first()
        attendances_data.append({'reg': reg, 'att': att})

    summary = {
        'total':    EventRegistration.objects.count(),
        'pending':  Attendance.objects.filter(status=Attendance.STATUS_PENDING).count(),
        'verified': Attendance.objects.filter(status=Attendance.STATUS_VERIFIED).count(),
        'rejected': Attendance.objects.filter(status=Attendance.STATUS_REJECTED).count(),
        'not_attended': EventRegistration.objects.filter(attendances__isnull=True).count(),
    }

    status_choices = list(Attendance.STATUS_CHOICES)
    status_choices.append(('not_attended', 'Belum Absen'))

    # Daftar nama event untuk dropdown filter, diambil dari data registrasi
    # itu sendiri (bukan dari tabel Event) supaya event lama yang FK-nya
    # kosong tetap muncul sebagai pilihan filter yang valid.
    event_options = list(
        EventRegistration.objects.order_by('event_name')
        .values_list('event_name', flat=True)
        .distinct()
    )

    # ---------- 2) ATTENDANCE ANALYTICS (Step 8) ----------
    period  = request.GET.get('period', 'monthly')
    year    = request.GET.get('year')
    quarter = request.GET.get('quarter')
    month   = request.GET.get('month')

    period_start, period_end, period_trunc, period_label = _resolve_period_range(period, year, quarter, month)

    registrations_qs = EventRegistration.objects.filter(
        registered_at__range=(period_start, period_end)
    )
    attendance_period_qs = Attendance.objects.filter(
        attendance_timestamp__range=(period_start, period_end)
    )

    total_registrations = registrations_qs.count()
    total_attendance_period = attendance_period_qs.count()
    verified_attendance_period = attendance_period_qs.filter(status=Attendance.STATUS_VERIFIED).count()
    pending_attendance_period  = attendance_period_qs.filter(status=Attendance.STATUS_PENDING).count()
    rejected_attendance_period = attendance_period_qs.filter(status=Attendance.STATUS_REJECTED).count()

    if total_registrations:
        attendance_rate = round((total_attendance_period / total_registrations) * 100, 1)
        no_show_rate = round(
            ((total_registrations - total_attendance_period) / total_registrations) * 100, 1
        )
    else:
        attendance_rate = 0
        no_show_rate = 0

    attendance_series_qs = (
        attendance_period_qs
        .annotate(period_bucket=period_trunc('attendance_timestamp'))
        .values('period_bucket')
        .annotate(count=Count('attendance_id'))
        .order_by('period_bucket')
    )
    attendance_chart_labels = [
        row['period_bucket'].strftime('%d %b') if period_trunc is TruncDay else row['period_bucket'].strftime('%b %Y')
        for row in attendance_series_qs
    ]
    attendance_chart_values = [row['count'] for row in attendance_series_qs]

    # ---------- 3) PARTICIPATION ANALYTICS (Step 9) ----------
    granularity = request.GET.get('granularity', 'monthly')  # monthly | quarterly | yearly
    p_year = request.GET.get('p_year')
    p_year = int(p_year) if p_year else timezone.now().year

    if granularity == 'yearly':
        p_trunc = TruncYear
        p_date_fmt = '%Y'
        participation_qs = EventRegistration.objects.all()
    elif granularity == 'quarterly':
        p_trunc = TruncQuarter
        p_date_fmt = None  # diformat manual di bawah
        participation_qs = EventRegistration.objects.filter(registered_at__year=p_year)
    else:
        p_trunc = TruncMonth
        p_date_fmt = '%b %Y'
        participation_qs = EventRegistration.objects.filter(registered_at__year=p_year)

    participation_series = list(
        participation_qs
        .annotate(bucket=p_trunc('registered_at'))
        .values('bucket')
        .annotate(count=Count('registration_id'))
        .order_by('bucket')
    )

    participation_labels = []
    participation_values = []
    for row in participation_series:
        bucket = row['bucket']
        if granularity == 'quarterly':
            q_num = (bucket.month - 1) // 3 + 1
            participation_labels.append(f"Q{q_num} {bucket.year}")
        else:
            participation_labels.append(bucket.strftime(p_date_fmt))
        participation_values.append(row['count'])

    growth_percent = None
    if len(participation_values) >= 2 and participation_values[-2] > 0:
        growth_percent = round(
            ((participation_values[-1] - participation_values[-2]) / participation_values[-2]) * 100, 1
        )
    elif len(participation_values) >= 2 and participation_values[-2] == 0 and participation_values[-1] > 0:
        growth_percent = 100.0

    total_participants = sum(participation_values)

    # ---------- CONTEXT GABUNGAN ----------
    context = {
        # Attendance Verification
        'attendances':    attendances_data,
        'summary':        summary,
        'current_status': current_status,
        'status_choices': status_choices,
        'current_event':  current_event,
        'event_options':  event_options,

        # Attendance Analytics (Step 8)
        'period': period,
        'period_label': period_label,
        'selected_year': int(year) if year else timezone.now().year,
        'selected_quarter': int(quarter) if quarter else None,
        'selected_month': int(month) if month else None,
        'attendance_kpi': {
            'total_registrations': total_registrations,
            'total_attendance': total_attendance_period,
            'verified_attendance': verified_attendance_period,
            'pending_attendance': pending_attendance_period,
            'rejected_attendance': rejected_attendance_period,
            'attendance_rate': attendance_rate,
            'no_show_rate': no_show_rate,
        },
        'attendance_chart_labels': json.dumps(attendance_chart_labels),
        'attendance_chart_values': json.dumps(attendance_chart_values),
        'year_choices': range(timezone.now().year - 3, timezone.now().year + 1),
        'quarter_choices': [1, 2, 3, 4],
        'month_choices': list(enumerate(
            ['Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
             'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'],
            start=1
        )),

        # Participation Analytics (Step 9)
        'granularity': granularity,
        'selected_p_year': p_year,
        'participation_labels': json.dumps(participation_labels),
        'participation_values': json.dumps(participation_values),
        'total_participants': total_participants,
        'growth_percent': growth_percent,
        'latest_period_label': participation_labels[-1] if participation_labels else None,
        'latest_period_count': participation_values[-1] if participation_values else 0,
    }
    return render(request, 'cms/admin_attendance_list.html', context)

@admin_login_required
def admin_verify_attendance(request, attendance_id):
    if request.method == 'POST':
        att = get_object_or_404(Attendance, attendance_id=attendance_id)
        att.status = Attendance.STATUS_VERIFIED
        att.verified_at = timezone.now()
        att.save()
        log_admin_activity(f"Attendance #{attendance_id} ({att.participant_email}) diverifikasi")

        # ================= RECOMPUTE GENUINE FEEDBACK =================
        # Attendance baru saja verified -> feedback lama milik email+event
        # yang sama, yang saat dikirim sudah terdeteksi LLM sebagai
        # feedback asli (is_feedback_detected=True) tapi belum ditandai
        # is_genuine_feedback (karena waktu itu attendance belum verified),
        # sekarang boleh dihitung genuine. Pakai is_feedback_detected yang
        # sudah tersimpan supaya tidak perlu manggil ulang LLM/re-classify.
        #
        # Tanpa event terkait, attendance_ok sudah otomatis True saat
        # feedback dibuat (lihat chat_with_ai), jadi tidak ada yang perlu
        # direcompute untuk feedback tanpa event_id.
        event_id = att.event_registration.event_id if att.event_registration else None

        if event_id:
            pending_feedbacks = list(Feedback.objects.filter(
                participant_email__iexact=att.participant_email,
                event_id=event_id,
                is_genuine_feedback=False,
                is_feedback_detected=True,
            ))

            for fb in pending_feedbacks:
                # Sentimen belum tersimpan untuk feedback yang tadinya
                # tidak genuine (lihat chat_with_ai: sentiment hanya
                # disimpan kalau is_genuine_feedback True). Re-classify
                # di sini pakai sentiment_analyzer lokal (bukan LLM Groq),
                # jadi tidak melanggar tujuan menghindari re-classify LLM.
                if fb.sentiment is None:
                    try:
                        fb.sentiment, _ = classify_feedback(fb.message)
                    except Exception:
                        pass
                fb.is_genuine_feedback = True
                fb.save(update_fields=['is_genuine_feedback', 'sentiment'])

            if pending_feedbacks:
                log_admin_activity(
                    f"Recompute {len(pending_feedbacks)} feedback ({att.participant_email}) "
                    f"jadi genuine setelah attendance #{attendance_id} diverifikasi"
                )

                # Rating rata-rata event bisa berubah kalau di antara
                # feedback yang baru jadi genuine ada yang punya rating.
                avg_rating = Feedback.objects.filter(
                    event_id=event_id,
                    rating__isnull=False,
                    is_genuine_feedback=True,
                ).aggregate(avg=Avg('rating'))['avg']

                Event.objects.filter(event_id=event_id).update(
                    rating=round(avg_rating, 1) if avg_rating is not None else None
                )

    return redirect('admin_attendance')


@admin_login_required
def admin_reject_attendance(request, attendance_id):
    if request.method == 'POST':
        att = get_object_or_404(Attendance, attendance_id=attendance_id)
        att.status = Attendance.STATUS_REJECTED
        att.save()
        log_admin_activity(f"Attendance #{attendance_id} ({att.participant_email}) ditolak")
    return redirect('admin_attendance')