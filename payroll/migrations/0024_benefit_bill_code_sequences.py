from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0023_alter_benefitattachment_date_created_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE SEQUENCE IF NOT EXISTS benefit_code_seq;",
            reverse_sql="DROP SEQUENCE IF EXISTS benefit_code_seq;",
        ),
        migrations.RunSQL(
            sql="""
            DO $$
            DECLARE max_val BIGINT;
            BEGIN
                SELECT COALESCE(MAX(
                    CASE WHEN code ~ '^[0-9]+$' THEN code::BIGINT
                         WHEN code ~ '-([0-9]+)$' THEN (regexp_match(code, '-([0-9]+)$'))[1]::BIGINT
                         ELSE 0 END
                ), 0) INTO max_val FROM payroll_benefitconsumption;
                IF max_val > 0 THEN PERFORM setval('benefit_code_seq', max_val); END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
            ALTER TABLE payroll_benefitconsumption
            ALTER COLUMN code
            SET DEFAULT 'BEN-' || to_char(now(), 'YY') || '-' || lpad(nextval('benefit_code_seq')::text, 7, '0');
            """,
            reverse_sql="ALTER TABLE payroll_benefitconsumption ALTER COLUMN code DROP DEFAULT;",
        ),
    ]
