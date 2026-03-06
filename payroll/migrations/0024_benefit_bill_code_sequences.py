from django.db import migrations


# ── PostgreSQL ────────────────────────────────────────────────────────────────

def _pg_apply(schema_editor):
    schema_editor.execute("CREATE SEQUENCE IF NOT EXISTS benefit_code_seq;")
    schema_editor.execute("""
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
    """)
    schema_editor.execute("""
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
    """)


def _pg_reverse(schema_editor):
    schema_editor.execute("DROP TRIGGER IF EXISTS benefit_code_trigger ON payroll_benefitconsumption;")
    schema_editor.execute("DROP FUNCTION IF EXISTS set_benefit_code();")
    schema_editor.execute("DROP SEQUENCE IF EXISTS benefit_code_seq;")


# ── MSSQL (SQL Server 2012+) ──────────────────────────────────────────────────

def _mssql_apply(schema_editor):
    # 1. Create sequence (idempotent)
    schema_editor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.sequences WHERE object_id = OBJECT_ID('benefit_code_seq'))
            EXEC('CREATE SEQUENCE benefit_code_seq AS BIGINT START WITH 1 INCREMENT BY 1');
    """)
    # 2. Advance sequence past the highest code already in the table
    schema_editor.execute("""
        DECLARE @max_val BIGINT = 0;
        SELECT @max_val = COALESCE(MAX(
            CASE
                WHEN [code] LIKE '%-%'
                    THEN TRY_CAST(
                        REVERSE(LEFT(REVERSE([code]), CHARINDEX('-', REVERSE([code])) - 1))
                        AS BIGINT)
                WHEN [code] NOT LIKE '%[^0-9]%' AND LEN([code]) > 0
                    THEN TRY_CAST([code] AS BIGINT)
                ELSE 0
            END
        ), 0) FROM [payroll_benefitconsumption];
        IF @max_val > 0
            EXEC(N'ALTER SEQUENCE benefit_code_seq RESTART WITH ' + CAST(@max_val + 1 AS NVARCHAR(20)));
    """)
    # 3. Drop old trigger if present (must precede CREATE TRIGGER)
    schema_editor.execute("""
        IF OBJECT_ID('benefit_code_trigger', 'TR') IS NOT NULL
            DROP TRIGGER [benefit_code_trigger];
    """)
    # 4. Create trigger — must be first statement in its batch.
    # INSTEAD OF INSERT intercepts the row before it lands on disk so we can
    # substitute a generated code inline, avoiding a redundant post-INSERT UPDATE.
    schema_editor.execute("""
        CREATE TRIGGER [benefit_code_trigger]
        ON [payroll_benefitconsumption]
        INSTEAD OF INSERT
        AS
        BEGIN
            SET NOCOUNT ON;
            INSERT INTO [payroll_benefitconsumption] (
                [UUID], [code], [individual_id], [photo],
                [DateDue], [Receipt], [Amount], [Type],
                [status],
                [date_valid_from], [date_valid_to],
                [date_created], [date_updated],
                [user_created_id], [user_updated_id],
                [version], [is_deleted], [json_ext]
            )
            SELECT
                i.[UUID],
                CASE
                    WHEN i.[code] IS NULL OR i.[code] = ''
                        THEN 'BEN-' + RIGHT(CONVERT(VARCHAR(4), YEAR(GETDATE())), 2) + '-'
                             + RIGHT('0000000000' + CAST(NEXT VALUE FOR benefit_code_seq AS VARCHAR(20)), 10)
                    ELSE i.[code]
                END,
                i.[individual_id],
                i.[photo],
                i.[DateDue],
                i.[Receipt],
                i.[Amount],
                i.[Type],
                i.[status],
                i.[date_valid_from],
                i.[date_valid_to],
                i.[date_created],
                i.[date_updated],
                i.[user_created_id],
                i.[user_updated_id],
                i.[version],
                i.[is_deleted],
                i.[json_ext]
            FROM inserted i;
        END
    """)


def _mssql_reverse(schema_editor):
    schema_editor.execute("""
        IF OBJECT_ID('benefit_code_trigger', 'TR') IS NOT NULL
            DROP TRIGGER [benefit_code_trigger];
    """)
    schema_editor.execute("""
        IF OBJECT_ID('benefit_code_seq', 'SO') IS NOT NULL
            DROP SEQUENCE [benefit_code_seq];
    """)


# ── Migration entry point ─────────────────────────────────────────────────────

def apply_benefit_code_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        _pg_apply(schema_editor)
    elif vendor == 'microsoft':
        _mssql_apply(schema_editor)


def reverse_benefit_code_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        _pg_reverse(schema_editor)
    elif vendor == 'microsoft':
        _mssql_reverse(schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0023_alter_benefitattachment_date_created_and_more'),
    ]

    operations = [
        migrations.RunPython(apply_benefit_code_trigger, reverse_benefit_code_trigger),
    ]
