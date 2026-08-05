from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from .models import Event, EventRegistration, Attendance


# ─────────────────────────────────────────────
# EventRegistration Inline — shown inside Event admin page
# ─────────────────────────────────────────────
class EventRegistrationInline(admin.TabularInline):
    """Tampilkan daftar peserta terdaftar langsung di halaman edit Event."""
    model = EventRegistration
    extra = 0
    fields = ('full_name', 'email', 'phone', 'province', 'territory', 'registered_at')
    readonly_fields = ('registered_at',)
    can_delete = False
    show_change_link = True


# ─────────────────────────────────────────────
# Event Admin — full lifecycle management
# ─────────────────────────────────────────────
@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """
    Admin panel untuk Event dengan Event Lifecycle Management.

    Fitur:
    - Filter dan tampilan per status (Draft / Published / Ongoing / Completed / Archived)
    - Quick-action: publish, set ongoing, complete, archive
    - Inline peserta terdaftar
    - Badge status berwarna di list
    """

    # ── List view ───────────────────────────────────────────────
    list_display   = (
        'event_id', 'event_name', 'colored_status', 'event_date',
        'person_in_charge', 'capacity_display', 'registration_deadline',
        'is_attendance_open_display', 'created_at',
    )
    list_filter    = ('status', 'event_date')
    search_fields  = ('event_name', 'location', 'person_in_charge')
    ordering       = ('-event_date',)
    date_hierarchy = 'event_date'
    list_per_page  = 30
    actions        = [
        'action_publish', 'action_set_ongoing',
        'action_complete', 'action_archive', 'action_revert_draft',
    ]

    # ── Detail / Edit view ──────────────────────────────────────
    fieldsets = (
        ('Informasi Dasar', {
            'fields': ('event_name', 'description', 'location', 'event_date', 'event_time', 'image_url'),
        }),
        ('Lifecycle & Status', {
            'fields': ('status', 'person_in_charge', 'capacity'),
            'description': (
                'Event baru otomatis berstatus <strong>Draft</strong>. '
                'Hanya event <strong>Published</strong> yang tampil di halaman Explore publik. '
                'Event <strong>Completed</strong> masih bisa dilihat peserta. '
                'Event <strong>Archived</strong> hanya bisa diakses admin.'
            ),
        }),
        ('Jadwal Registrasi & Absensi', {
            'fields': ('registration_deadline', 'attendance_open_time', 'attendance_close_time'),
            'classes': ('collapse',),
            'description': (
                'Biarkan kosong jika tidak diperlukan. '
                'Registrasi otomatis tutup setelah <em>Registration Deadline</em>. '
                'Absensi hanya bisa dilakukan antara <em>Open Time</em> dan <em>Close Time</em>.'
            ),
        }),
        ('Metadata', {
            'fields': ('rating', 'created_by', 'created_at'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('created_at',)

    # ── Computed columns ────────────────────────────────────────
    @admin.display(description='Status', ordering='status')
    def colored_status(self, obj):
        colours = {
            Event.STATUS_DRAFT:     ('#94a3b8', '#fff'),
            Event.STATUS_PUBLISHED: ('#22c55e', '#fff'),
            Event.STATUS_ONGOING:   ('#3b82f6', '#fff'),
            Event.STATUS_COMPLETED: ('#8b5cf6', '#fff'),
            Event.STATUS_ARCHIVED:  ('#ef4444', '#fff'),
        }
        bg, fg = colours.get(obj.status, ('#e2e8f0', '#000'))
        label  = obj.get_status_display()
        return format_html(
            '<span style="background:{};color:{};padding:3px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, fg, label,
        )

    @admin.display(description='Kapasitas')
    def capacity_display(self, obj):
        if obj.capacity is None:
            return '∞'
        pct = obj.registered_count / obj.capacity * 100 if obj.capacity else 0
        colour = '#ef4444' if pct >= 90 else '#f59e0b' if pct >= 60 else '#22c55e'
        return format_html(
            '<span style="color:{}">{} / {}</span>',
            colour, obj.registered_count, obj.capacity,
        )

    @admin.display(description='Absensi Terbuka?', boolean=True)
    def is_attendance_open_display(self, obj):
        return obj.is_attendance_open

    # ── Bulk actions ────────────────────────────────────────────
    @admin.action(description='✅ Publish event terpilih')
    def action_publish(self, request, queryset):
        updated = queryset.update(status=Event.STATUS_PUBLISHED)
        self.message_user(request, f'{updated} event dipublikasikan.')

    @admin.action(description='🔵 Set Ongoing')
    def action_set_ongoing(self, request, queryset):
        updated = queryset.update(status=Event.STATUS_ONGOING)
        self.message_user(request, f'{updated} event diset ke Ongoing.')

    @admin.action(description='🟣 Tandai Completed')
    def action_complete(self, request, queryset):
        updated = queryset.update(status=Event.STATUS_COMPLETED)
        self.message_user(request, f'{updated} event ditandai Completed.')

    @admin.action(description='🔴 Archive event terpilih')
    def action_archive(self, request, queryset):
        updated = queryset.update(status=Event.STATUS_ARCHIVED)
        self.message_user(request, f'{updated} event diarsipkan.')

    @admin.action(description='⬛ Kembalikan ke Draft')
    def action_revert_draft(self, request, queryset):
        updated = queryset.update(status=Event.STATUS_DRAFT)
        self.message_user(request, f'{updated} event dikembalikan ke Draft.')


# ─────────────────────────────────────────────
# EventRegistration Admin
# ─────────────────────────────────────────────
@admin.register(EventRegistration)
class EventRegistrationAdmin(admin.ModelAdmin):
    """Tampilan admin untuk peserta event."""
    list_display   = (
        'registration_id', 'full_name', 'email', 'event_name',
        'province', 'territory', 'phone', 'registered_at',
    )
    list_filter    = ('province', 'territory', 'event_name', 'registered_at')
    search_fields  = ('full_name', 'email', 'event_name', 'province', 'territory')
    readonly_fields = ('registered_at',)
    ordering       = ('-registered_at',)
    list_per_page  = 50


# ─────────────────────────────────────────────
# Attendance Admin
# ─────────────────────────────────────────────
@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    """Verifikasi kehadiran peserta."""
    list_display   = (
        'attendance_id', 'participant_email', 'event_name_display',
        'colored_attendance_status', 'attendance_timestamp', 'verified_at',
    )
    list_filter    = ('status', 'attendance_timestamp')
    search_fields  = ('participant_email', 'event_registration__event_name')
    readonly_fields = ('attendance_timestamp',)
    ordering       = ('-attendance_timestamp',)
    list_per_page  = 50
    actions        = ['action_verify', 'action_reject']

    @admin.display(description='Event')
    def event_name_display(self, obj):
        return obj.event_registration.event_name

    @admin.display(description='Status')
    def colored_attendance_status(self, obj):
        colours = {
            Attendance.STATUS_PENDING:  ('#f59e0b', '#fff'),
            Attendance.STATUS_VERIFIED: ('#22c55e', '#fff'),
            Attendance.STATUS_REJECTED: ('#ef4444', '#fff'),
        }
        bg, fg = colours.get(obj.status, ('#e2e8f0', '#000'))
        return format_html(
            '<span style="background:{};color:{};padding:3px 10px;'
            'border-radius:999px;font-size:12px;font-weight:600;">{}</span>',
            bg, fg, obj.get_status_display(),
        )

    @admin.action(description='✅ Verifikasi kehadiran terpilih')
    def action_verify(self, request, queryset):
        now = timezone.now()
        updated = queryset.update(status=Attendance.STATUS_VERIFIED, verified_at=now)
        self.message_user(request, f'{updated} kehadiran diverifikasi.')

    @admin.action(description='❌ Tolak kehadiran terpilih')
    def action_reject(self, request, queryset):
        updated = queryset.update(status=Attendance.STATUS_REJECTED)
        self.message_user(request, f'{updated} kehadiran ditolak.')