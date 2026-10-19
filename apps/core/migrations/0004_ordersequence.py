"""Add the locked counter used to allocate annual order references."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_alter_modulegate_options")]

    operations = [
        migrations.CreateModel(
            name="OrderSequence",
            fields=[
                (
                    "financial_year",
                    models.CharField(max_length=4, primary_key=True, serialize=False),
                ),
                ("last_sequence", models.TextField(default="0")),
            ],
            options={"ordering": ["financial_year"]},
        ),
    ]
