from django.conf import settings
from django.db import models


class Branch(models.Model):
    name = models.CharField(max_length=90, unique=True)


class Store(models.Model):
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT)
    name = models.CharField(max_length=90, unique=True)
    lat = models.FloatField()
    lon = models.FloatField()


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)

    additional_stores = models.ManyToManyField(
        Store, blank=True, related_name="additional_profiles"
    )

    def linked_stores(self):
        return (
            Store.objects.filter(
                models.Q(pk=self.store_id) | models.Q(additional_profiles=self)
            )
            .select_related("branch")
            .order_by("branch__name", "name")
            .distinct()
        )


class AccountChange(models.Model):
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="account_changes",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="account_edits"
    )
    created = models.DateTimeField(auto_now_add=True)
    before = models.JSONField(default=dict)
    after = models.JSONField()


class Product(models.Model):
    sku = models.CharField(max_length=24, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=40)
    variant = models.CharField(max_length=50)
    image = models.CharField(max_length=100)


class Stock(models.Model):
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    available = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "product"], name="stock_store_product"
            )
        ]


class Transfer(models.Model):
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="outgoing")
    destination = models.ForeignKey(
        Store, on_delete=models.PROTECT, related_name="incoming"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    status = models.CharField(
        max_length=30, default="Aguardando aprovação", db_index=True
    )
    invoice = models.CharField(max_length=100, blank=True)
    token = models.UUIDField(unique=True)
    created = models.DateTimeField(auto_now_add=True)


class Receipt(models.Model):
    transfer = models.OneToOneField(Transfer, on_delete=models.PROTECT)
    image = models.BinaryField()


class Event(models.Model):
    transfer = models.ForeignKey(
        Transfer, on_delete=models.PROTECT, related_name="events"
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=30)
    created = models.DateTimeField(auto_now_add=True)
