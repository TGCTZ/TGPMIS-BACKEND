from django.db import migrations, models


def move_partly_paid_bills_to_pending(apps, schema_editor):
    """Keep historical underpayments recorded while making their bill unpaid."""
    Bill = apps.get_model("billing", "Bill")
    Bill.objects.using(schema_editor.connection.alias).filter(
        status="partially_paid"
    ).update(status="pending")


class Migration(migrations.Migration):
    dependencies = [("billing", "0005_enforce_tzs_currency")]

    operations = [
        migrations.RunPython(
            move_partly_paid_bills_to_pending,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="bill",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("paid", "Paid"),
                    ("cancelled", "Cancelled"),
                    ("expired", "Expired"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
