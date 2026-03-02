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
            CREATE OR REPLACE FUNCTION set_benefit_code()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.code IS NULL OR NEW.code = '' THEN
                    NEW.code := 'BEN-' || to_char(now(), 'YY') || '-'
                                || lpad(nextval('benefit_code_seq')::text, 10, '0');
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS benefit_code_trigger ON payroll_benefitconsumption;
            CREATE TRIGGER benefit_code_trigger
                BEFORE INSERT ON payroll_benefitconsumption
                FOR EACH ROW EXECUTE FUNCTION set_benefit_code();
            """,
            reverse_sql="""
            DROP TRIGGER IF EXISTS benefit_code_trigger ON payroll_benefitconsumption;
            DROP FUNCTION IF EXISTS set_benefit_code();
            """,
        ),
    ]
