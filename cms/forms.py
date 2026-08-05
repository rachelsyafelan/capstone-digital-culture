from django import forms

from .models import Event, EventRegistration


def get_missing_required_fields_for_publish(data):
    """
    Same required-for-publish rule as Event.get_missing_required_fields(),
    but works on a plain dict of raw field values instead of a saved Event
    instance. Used by admin_add_event(), where the values are still fresh
    off request.POST and no Event row exists yet to run the instance
    method against.

    `data` keys should match Event.REQUIRED_FOR_PUBLISH_FIELDS field names
    (event_name, description, location, event_date, event_time, end_time,
    image_url). Capacity and person_in_charge are never checked here either
    — always optional, same as the instance method.
    """
    missing = []
    for field_name in Event.REQUIRED_FOR_PUBLISH_FIELDS:
        value = data.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(Event.REQUIRED_FIELD_LABELS.get(field_name, field_name))
    return missing


class EventRegistrationForm(forms.ModelForm):
    """
    Form pendaftaran peserta event TANPA login.

    Catatan desain:
    - event_id & event_name TIDAK ada di form. Keduanya di-set oleh view dari
      objek Event (berdasarkan URL), bukan dari input user, supaya tidak bisa
      dipalsukan dan selalu konsisten dengan event yang sedang dibuka.
    - Email wajib, dinormalisasi (trim + lower-case), dan dicek agar satu email
      hanya bisa mendaftar satu kali per event. Pengecekan di form ini untuk
      pesan yang ramah; jaminan sebenarnya tetap pada UniqueConstraint database
      + penanganan IntegrityError di view (untuk kasus balapan / race).
    """

    class Meta:
        model = EventRegistration
        fields = ['full_name', 'email', 'province', 'territory', 'phone', 'organization']
        error_messages = {
            'full_name': {
                'required': 'Nama lengkap wajib diisi.',
            },
            'email': {
                'required': 'Email wajib diisi.',
                'invalid': 'Format email tidak valid.',
            },
        }

    def __init__(self, *args, event=None, **kwargs):
        # `event` di-inject oleh view supaya bisa cek duplikasi per-event.
        self.event = event
        super().__init__(*args, **kwargs)

        # Hanya nama & email yang wajib; sisanya opsional.
        self.fields['full_name'].required = True
        self.fields['email'].required = True
        for name in ('province', 'territory', 'phone', 'organization'):
            self.fields[name].required = False

    def clean_full_name(self):
        return (self.cleaned_data.get('full_name') or '').strip()

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            # Pengaman tambahan; EmailField.required sudah menangani kasus kosong.
            raise forms.ValidationError('Email wajib diisi.')
        return email

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get('email')

        # Aturan bisnis: satu email hanya boleh mendaftar satu kali per event.
        if email and self.event is not None:
            qs = EventRegistration.objects.filter(
                event_id=self.event.event_id,
                email__iexact=email,
            )
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error(
                    'email',
                    'Email ini sudah terdaftar untuk event ini. '
                    'Satu email hanya dapat mendaftar satu kali per event.',
                )

        return cleaned