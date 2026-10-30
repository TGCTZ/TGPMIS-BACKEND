"""Make access to the legacy system assignable through the role matrix."""

from django.db import migrations


def grant_full_access_roles(apps, schema_editor):
    """Keep the administrator roles' existing full-access behavior."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    database = schema_editor.connection.alias

    content_type, _ = ContentType.objects.using(database).get_or_create(
        app_label="core", model="modulegate"
    )
    permission, _ = Permission.objects.using(database).get_or_create(
        content_type=content_type,
        codename="access_legacy_system",
        defaults={"name": "Can access the legacy system"},
    )

    full_access_groups = Group.objects.using(database).filter(
        name__in=("admin", "superadmin")
    )
    for group in full_access_groups:
        group.permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [("core", "0004_ordersequence")]

    operations = [
        migrations.AlterModelOptions(
            name="modulegate",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": [
                    ("module_orders", "Can access the orders module"),
                    ("module_identification", "Can access the identification module"),
                    ("module_billing", "Can access the billing module"),
                    ("module_reports", "Can access the reports module"),
                    ("report_financial", "Can access financial reports"),
                    ("report_operational", "Can access operational reports"),
                    ("module_certificates", "Can access the certificates module"),
                    ("module_reference", "Can access the reference-data module"),
                    ("module_user", "Can access the user module"),
                    ("module_settings", "Can access the settings module"),
                    ("module_audit", "Can access the audit module"),
                    ("access_legacy_system", "Can access the legacy system"),
                ],
            },
        ),
        migrations.RunPython(grant_full_access_roles, migrations.RunPython.noop),
    ]
