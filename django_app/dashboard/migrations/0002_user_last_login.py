from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0001_initial")]
    operations = [migrations.AddField(model_name="user", name="last_login", field=models.DateTimeField(blank=True, null=True))]
