from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


# =====================================
# CORE VALUES
# =====================================

class CoreValue(models.Model):
    core_value_id = models.AutoField(primary_key=True)
    core_value_name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'core_values'
        managed = False

    def __str__(self):
        return self.core_value_name


# =====================================
# EVENTS
# =====================================

class Event(models.Model):

    STATUS_DRAFT     = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_ONGOING   = 'ongoing'
    STATUS_COMPLETED = 'completed'
    STATUS_ARCHIVED  = 'archived'

    STATUS_CHOICES = [
        (STATUS_DRAFT,     'Draft'),
        (STATUS_PUBLISHED, 'Published'),
        (STATUS_ONGOING,   'Ongoing'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_ARCHIVED,  'Archived'),
    ]

    event_id    = models.AutoField(primary_key=True)
    event_name  = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    location    = models.CharField(max_length=255, null=True, blank=True)
    event_date  = models.DateField(null=True, blank=True)
    # event_time is the start time; end_time completes the one-day event window.
    event_time  = models.TimeField(null=True, blank=True)
    end_time    = models.TimeField(null=True, blank=True)
    rating      = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    image_url   = models.TextField(null=True, blank=True)
    created_by  = models.IntegerField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    # ── New Lifecycle Fields ──────────────────────────────────────
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
    )
    registration_deadline = models.DateTimeField(null=True, blank=True)
    attendance_open_time  = models.DateTimeField(null=True, blank=True)
    attendance_close_time = models.DateTimeField(null=True, blank=True)
    capacity              = models.PositiveIntegerField(null=True, blank=True)
    person_in_charge      = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = 'events'
        managed = True   # Changed to True so Django manages migrations

    def __str__(self):
        return self.event_name

    # ── Helpers ──────────────────────────────────────────────────
    @property
    def is_visible_to_participants(self):
        """Published + Completed events visible to participants."""
        return self.status in (self.STATUS_PUBLISHED, self.STATUS_COMPLETED, self.STATUS_ONGOING)

    @property
    def is_registration_open(self):
        now = timezone.now()
        if self.registration_deadline and now > self.registration_deadline:
            return False
        return self.status == self.STATUS_PUBLISHED

    @property
    def is_attendance_open(self):
        now = timezone.now()
        if not self.attendance_open_time:
            return False
        if now < self.attendance_open_time:
            return False
        if self.attendance_close_time and now > self.attendance_close_time:
            return False
        return True

    @property
    def registered_count(self):
        return EventRegistration.objects.filter(event_id=self.event_id).count()

    @property
    def is_full(self):
        if self.capacity is None:
            return False
        return self.registered_count >= self.capacity

    # ════════════════════════════════════════════════════════════════
    # LIFECYCLE GATE — Draft ⇄ Published completeness check
    # ════════════════════════════════════════════════════════════════
    # Business rule: an event can only be Published once every required
    # field on the Event Form is filled in — including Registration
    # Deadline, Attendance Open Time, and Attendance Close Time. Capacity
    # and Person in Charge (PIC) are explicitly EXCLUDED — they stay
    # optional forever, an event can be published with either (or both)
    # left blank.
    #
    # A new/edited event with any of these fields still empty is always
    # saved as Draft. If an admin explicitly asks to Publish while
    # something required is missing, that's a hard validation error —
    # views.py rejects the save entirely and tells the admin exactly what
    # to fill in (see admin_add_event / admin_edit_event /
    # admin_change_event_status), rather than silently saving as Draft.
    #
    # This list is the single source of truth for "required" — forms.py
    # and views.py both call get_missing_required_fields() /
    # is_ready_to_publish below instead of hard-coding field names, so the
    # rule can't drift out of sync between the three admin entry points.
    REQUIRED_FOR_PUBLISH_FIELDS = [
        'event_name', 'description', 'location',
        'event_date', 'event_time', 'end_time', 'image_url',
        'registration_deadline', 'attendance_open_time', 'attendance_close_time',
    ]

    REQUIRED_FIELD_LABELS = {
        'event_name':  'Judul Event',
        'description': 'Deskripsi Event',
        'location':    'Lokasi',
        'event_date':  'Tanggal Event',
        'event_time':  'Waktu Mulai',
        'end_time':    'Waktu Selesai',
        'image_url':   'Gambar Event',
        'registration_deadline': 'Registration Deadline',
        'attendance_open_time':  'Attendance Open Time',
        'attendance_close_time': 'Attendance Close Time',
    }

    def get_missing_required_fields(self):
        """
        Return the human-readable labels of required fields that are
        still empty. Capacity & PIC are intentionally never checked here.
        An empty list means the event is complete enough to Publish.
        """
        missing = []
        for field_name in self.REQUIRED_FOR_PUBLISH_FIELDS:
            value = getattr(self, field_name, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(self.REQUIRED_FIELD_LABELS.get(field_name, field_name))
        return missing

    @property
    def is_ready_to_publish(self):
        """True once every required field (Capacity/PIC excluded) is filled in."""
        return not self.get_missing_required_fields()


class EventCoreValue(models.Model):
    event       = models.ForeignKey(Event,     on_delete=models.CASCADE, db_column='event_id')
    core_value  = models.ForeignKey(CoreValue, on_delete=models.CASCADE, db_column='core_value_id')

    class Meta:
        db_table = 'event_core_values'
        managed = False


class EventRegistration(models.Model):
    """
    Pendaftaran peserta event — TANPA login.

    Email berperan sebagai identitas unik peserta. Kombinasi (email, event)
    hanya boleh muncul satu kali: satu email = satu pendaftaran per event.
    """
    registration_id = models.AutoField(primary_key=True)

    event      = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_index=True,
    )
    event_name = models.CharField(max_length=255)

    full_name = models.CharField(max_length=255)
    email     = models.EmailField(max_length=255)
    province  = models.CharField(max_length=100, blank=True, null=True)
    territory = models.CharField(max_length=100, blank=True, null=True)
    phone        = models.CharField(max_length=50, blank=True, null=True)
    organization = models.CharField(max_length=255, blank=True, null=True)

    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'event_registrations'
        ordering = ['-registered_at']
        constraints = [
            models.UniqueConstraint(
                Lower('email'), 'event',
                name='uniq_lower_email_per_event',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} — {self.event_name}"

    # ── Participation status helper ─────────────────────────────
    PARTICIPATION_REGISTERED          = 'registered'
    PARTICIPATION_ATTENDANCE_SUBMITTED = 'attendance_submitted'
    PARTICIPATION_ATTENDANCE_VERIFIED  = 'attendance_verified'
    PARTICIPATION_FEEDBACK_SUBMITTED   = 'feedback_submitted'

    PARTICIPATION_LABELS = {
        PARTICIPATION_REGISTERED:           'Registered',
        PARTICIPATION_ATTENDANCE_SUBMITTED: 'Attendance Submitted',
        PARTICIPATION_ATTENDANCE_VERIFIED:  'Attendance Verified',
        PARTICIPATION_FEEDBACK_SUBMITTED:   'Feedback Submitted',
    }

    def get_participation_status(self):
        """
        Hitung status partisipasi tertinggi yang sudah dicapai peserta ini,
        urut dari yang paling awal (Registered) ke paling akhir
        (Feedback Submitted). Selalu dihitung ulang dari data terbaru di DB
        (bukan field tersimpan) supaya selalu akurat.

        Returns: dict {'code': str, 'label': str}
        """
        status_code = self.PARTICIPATION_REGISTERED

        attendance = self.attendances.order_by('-attendance_timestamp').first()
        if attendance is not None:
            if attendance.status == Attendance.STATUS_VERIFIED:
                status_code = self.PARTICIPATION_ATTENDANCE_VERIFIED
            else:
                # pending_verification ATAU rejected -> tetap dianggap
                # "Attendance Submitted" (sudah submit, terlepas hasil verifikasi)
                status_code = self.PARTICIPATION_ATTENDANCE_SUBMITTED

        feedback_exists = Feedback.objects.filter(
            event_id=self.event_id,
            participant_email__iexact=self.email,
            rating__isnull=False,
        ).exists()
        if feedback_exists:
            status_code = self.PARTICIPATION_FEEDBACK_SUBMITTED

        return {
            'code': status_code,
            'label': self.PARTICIPATION_LABELS[status_code],
        }


# =====================================
# NEWS EDITIONS
# =====================================
class NewsEdition(models.Model):
    edition_id   = models.AutoField(primary_key=True)
    edition_name = models.CharField(max_length=100)

    class Meta:
        db_table = 'news_editions'
        managed = False

    def __str__(self):
        return self.edition_name

class News(models.Model):
    news_id    = models.AutoField(primary_key=True)
    title      = models.CharField(max_length=255, null=True, blank=True)
    content    = models.TextField(null=True, blank=True)
    image_url  = models.TextField(null=True, blank=True)
    image_file = models.ImageField(
        upload_to='news/',
        null=True,
        blank=True
    )
    edition    = models.ForeignKey(
        NewsEdition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='edition_id',
        related_name='news',
    )
    created_by = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'news'
        managed = False

    def __str__(self):
        return self.title or ''


# =====================================
# ATTRIBUTES
# =====================================

ATTRIBUTE_TYPES = (
    ('playbook', 'Playbook'),
    ('poster',   'Poster'),
    ('asset',    'Asset'),
    ('logo',     'Logo'),
    ('video',    'Video'),
)


class Attribute(models.Model):
    attribute_id   = models.AutoField(primary_key=True)
    attribute_name = models.CharField(max_length=255, null=True, blank=True)
    attribute_type = models.CharField(max_length=50, choices=ATTRIBUTE_TYPES, null=True, blank=True)
    file_url       = models.TextField(null=True, blank=True)
    created_by     = models.IntegerField(null=True, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'attributes'
        managed = False

    def __str__(self):
        return self.attribute_name or ''


# =====================================
# FEEDBACK
# =====================================

class Feedback(models.Model):

    SENTIMENT_CHOICES = [
        ('positive', 'Positive'),
        ('neutral',  'Neutral'),
        ('negative', 'Negative'),
    ]

    SOURCE_CHOICES = [
        ('web',    'Web'),
        ('mobile', 'Mobile'),
        ('api',    'API'),
    ]

    feedback_id     = models.AutoField(primary_key=True)

    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        db_column='event_id',
        null=True,
        blank=True
    )

    session_id      = models.CharField(max_length=100, blank=True, null=True)
    # Email peserta (opsional). Kolom ini SUDAH ADA secara fisik di database
    # (terlihat lewat `inspectdb`), tapi belum pernah didefinisikan di model
    # Django sebelumnya. Diisi otomatis dari session saat peserta yang sudah
    # teridentifikasi (lewat registrasi / cek status) mengirim feedback,
    # supaya status "Feedback Submitted" bisa dicek per-email di Event Detail.
    participant_email = models.EmailField(max_length=255, blank=True, null=True, db_index=True)
    message         = models.TextField()
    sentiment       = models.CharField(max_length=20, choices=SENTIMENT_CHOICES, null=True, blank=True)
    rating          = models.IntegerField(null=True, blank=True)
    source_platform = models.CharField(max_length=50, choices=SOURCE_CHOICES, default='web')
    created_at      = models.DateTimeField(auto_now_add=True)
    ai_response     = models.TextField(blank=True, null=True)
    # True hanya untuk pesan chat yang benar-benar feedback asli (bukan
    # sekadar tanya info) DAN, kalau terkait event, pengirimnya sudah
    # attendance-verified untuk event itu. Semua pesan tetap disimpan
    # (row ini tidak pernah None/kosong) supaya riwayat chat/history utuh;
    # field ini hanya penanda mana yang boleh dihitung sebagai statistik
    # feedback/sentiment asli.
    is_genuine_feedback = models.BooleanField(default=False)
    # Hasil deteksi LLM (IS_FEEDBACK:true/false dari Groq) disimpan APA
    # ADANYA di sini, terlepas dari status attendance pengirim saat itu.
    # Berbeda dengan `is_genuine_feedback` yang juga mensyaratkan
    # attendance sudah verified, kolom ini murni "apakah LLM mendeteksi
    # pesan ini sebagai feedback". Dipakai supaya saat attendance
    # diverifikasi belakangan (lihat admin_verify_attendance()), kita
    # bisa recompute is_genuine_feedback dengan query biasa tanpa perlu
    # manggil ulang LLM/re-classify pesan yang bersangkutan.
    is_feedback_detected = models.BooleanField(default=False)

    class Meta:
        db_table = 'feedback'
        managed = True   # Changed to True so Django manages the new `email` column
        ordering = ['-created_at']

    @staticmethod
    def email_has_verified_attendance(email, event=None):
        """
        Step 7 — Anonymous Feedback System.

        True hanya jika `email` punya minimal satu Attendance dengan
        status Verified. Kalau `event` diberikan, pengecekan dipersempit
        ke attendance pada event tersebut saja (feedback untuk event A
        harus attendance event A yang verified, bukan attendance event
        lain).
        """
        if not email:
            return False

        qs = Attendance.objects.filter(
            participant_email__iexact=email.strip(),
            status=Attendance.STATUS_VERIFIED,
        )
        if event is not None:
            qs = qs.filter(event_registration__event_id=event.event_id)
        return qs.exists()


# =====================================
# VISITOR LOGS
# =====================================

class VisitorLog(models.Model):
    log_id           = models.AutoField(primary_key=True)
    page_visited     = models.CharField(max_length=255, null=True, blank=True)
    visit_duration   = models.IntegerField(null=True, blank=True, help_text="Duration in seconds")
    engagement_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    visitor_ip       = models.GenericIPAddressField(null=True, blank=True)
    visited_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'visitor_logs'
        managed = True

    def __str__(self):
        return self.page_visited or ''


# =====================================
# ADMIN ACTIVITY
# =====================================

class AdminActivity(models.Model):
    activity_id = models.AutoField(primary_key=True)
    admin_id    = models.IntegerField(null=True, blank=True)
    activity    = models.TextField()
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'admin_activity'
        managed = False

    def __str__(self):
        return self.activity


# =====================================
# ATTENDANCE
# =====================================

class Attendance(models.Model):

    STATUS_PENDING  = 'pending_verification'
    STATUS_VERIFIED = 'verified'
    STATUS_REJECTED = 'rejected'

    STATUS_CHOICES = [
        (STATUS_PENDING,  'Pending Verification'),
        (STATUS_VERIFIED, 'Verified'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    attendance_id       = models.AutoField(primary_key=True)
    event_registration  = models.ForeignKey(
        EventRegistration,
        on_delete=models.CASCADE,
        related_name='attendances',
    )
    participant_email   = models.EmailField(max_length=255)
    photo_evidence      = models.ImageField(upload_to='attendance/', null=True, blank=True)
    latitude            = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    longitude           = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    attendance_timestamp = models.DateTimeField(auto_now_add=True)
    status              = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_PENDING)
    verified_at         = models.DateTimeField(null=True, blank=True)
    notes               = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'attendance'
        ordering = ['-attendance_timestamp']

    def __str__(self):
        return f"{self.participant_email} — {self.event_registration.event_name} [{self.status}]"
