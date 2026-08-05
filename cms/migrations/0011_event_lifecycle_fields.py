# Migration: Add Event Lifecycle Management fields
# Adds: status, registration_deadline, attendance_open_time,
#        attendance_close_time, capacity, person_in_charge
# Also switches Event from managed=False -> managed=True so Django owns the table.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cms', '0005_contactus'),
    ]

    operations = [
        # 1. Tell Django it now manages the events table
        migrations.AlterModelOptions(
            name='event',
            options={'ordering': ['-event_date']},
        ),

        # 2. status  ────────────────────────────────────────────────
        migrations.AddField(
            model_name='event',
            name='status',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('draft',     'Draft'),
                    ('published', 'Published'),
                    ('ongoing',   'Ongoing'),
                    ('completed', 'Completed'),
                    ('archived',  'Archived'),
                ],
                default='draft',
            ),
        ),

        # 3. registration_deadline  ─────────────────────────────────
        migrations.AddField(
            model_name='event',
            name='registration_deadline',
            field=models.DateTimeField(null=True, blank=True),
        ),

        # 4. attendance_open_time  ──────────────────────────────────
        migrations.AddField(
            model_name='event',
            name='attendance_open_time',
            field=models.DateTimeField(null=True, blank=True),
        ),

        # 5. attendance_close_time  ─────────────────────────────────
        migrations.AddField(
            model_name='event',
            name='attendance_close_time',
            field=models.DateTimeField(null=True, blank=True),
        ),

        # 6. capacity  ──────────────────────────────────────────────
        migrations.AddField(
            model_name='event',
            name='capacity',
            field=models.PositiveIntegerField(null=True, blank=True),
        ),

        # 7. person_in_charge  ──────────────────────────────────────
        migrations.AddField(
            model_name='event',
            name='person_in_charge',
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
    ]
