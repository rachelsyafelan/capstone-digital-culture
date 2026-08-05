from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cms', '0001_initial'),  # sesuaikan dengan nama migration awal kamu kalau beda
    ]

    operations = [
        migrations.AlterModelOptions(
            name='visitorlog',
            options={'db_table': 'visitor_logs'},
        ),
        migrations.RunSQL(
            sql="""
                CREATE TABLE IF NOT EXISTS visitor_logs (
                    log_id SERIAL PRIMARY KEY,
                    page_visited VARCHAR(255),
                    visit_duration INTEGER,
                    engagement_score NUMERIC(5,2),
                    visitor_ip VARCHAR(100),
                    visited_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                );
            """,
            reverse_sql="DROP TABLE IF EXISTS visitor_logs;",
            state_operations=[],  # state sudah benar dari migration sebelumnya, jadi kosongkan
        ),
    ]