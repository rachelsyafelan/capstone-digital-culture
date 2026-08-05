
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.functions.text


def null_orphaned_event_ids(apps, schema_editor):
    EventRegistration = apps.get_model('cms', 'EventRegistration')
    Event = apps.get_model('cms', 'Event')

    valid_ids = set(Event.objects.values_list('event_id', flat=True))
    orphaned = EventRegistration.objects.exclude(
        event_id__isnull=True
    ).exclude(event_id__in=valid_ids)

    count = orphaned.count()
    if count:
        orphaned.update(event_id=None)
        print(
            f"[migration 0004] {count} EventRegistration row(s) had an "
            f"event_id pointing to a deleted Event — nulled out so the new "
            f"FK constraint can be added. Review these rows if you need to "
            f"keep track of which event they originally belonged to."
        )


def noop_reverse(apps, schema_editor):
    # Nothing to reverse — we only nulled already-broken references.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('cms', '0003_alter_visitorlog_options'),
    ]

    operations = [
        # Step 1 — clean up any pre-existing orphaned references so adding
        # the FK constraint below doesn't fail on bad data.
        migrations.RunPython(null_orphaned_event_ids, noop_reverse),

        # Step 2 — drop the old constraint first (it references the
        # about-to-be-renamed field), then rename + retype the field, then
        # re-add the constraint pointing at the new field name. This mirrors
        # what Django's own autodetector produces for this kind of change.
        migrations.RemoveConstraint(
            model_name='eventregistration',
            name='uniq_lower_email_per_event',
        ),
        migrations.RenameField(
            model_name='eventregistration',
            old_name='event_id',
            new_name='event',
        ),
        migrations.AlterField(
            model_name='eventregistration',
            name='event',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to='cms.event',
            ),
        ),
        migrations.AddConstraint(
            model_name='eventregistration',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower('email'),
                models.F('event'),
                name='uniq_lower_email_per_event',
            ),
        ),

        # Step 3 — Feedback.event: CASCADE -> SET_NULL (preserve feedback
        # history when an event is deleted).
        migrations.AlterField(
            model_name='feedback',
            name='event',
            field=models.ForeignKey(
                blank=True,
                db_column='event_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='cms.event',
            ),
        ),

        # Step 4 — VisitorLog.visitor_ip: CharField -> GenericIPAddressField.
        migrations.AlterField(
            model_name='visitorlog',
            name='visitor_ip',
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
    ]