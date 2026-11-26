import os

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from estoque.models import Branch, Product, Profile, Stock, Store

ROLES = ("Vendedor", "Gerente", "Almoxarifado", "Transportadora")
STORES = [
    ("Paulista", -23.5614, -46.6559),
    ("Jardins", -23.5654, -46.6694),
    ("Pinheiros", -23.565, -46.691),
    ("Vila Mariana", -23.588, -46.634),
    ("Moema", -23.601, -46.664),
]
PRODUCTS = [
    (83, "Camisa xadrez azul", "Camisas", "Azul • M"),
    (177, "Vestido longo preto", "Vestidos", "Preto • M"),
    (90, "Tênis retrô colorido", "Calçados", "Multicolor • 40"),
    (172, "Bolsa azul estruturada", "Bolsas", "Azul • Único"),
    (85, "Camisa de flanela", "Camisas", "Xadrez • G"),
    (178, "Conjunto com corset", "Vestidos", "Preto • P"),
    (88, "Tênis de cano alto", "Calçados", "Vermelho • 41"),
    (173, "Bolsa de couro", "Bolsas", "Caramelo • Único"),
    (84, "Camiseta estampada", "Camisetas", "Preto • M"),
    (179, "Vestido com corset", "Vestidos", "Preto • M"),
    (89, "Chuteira de campo", "Calçados", "Preto • 39"),
    (174, "Bolsa de mão", "Bolsas", "Único"),
]


class Command(BaseCommand):
    @transaction.atomic
    def handle(self, *args, **kwargs):
        stores = []
        for name, lat, lon in STORES:
            branch, _ = Branch.objects.get_or_create(name="Filial " + name)
            store, _ = Store.objects.get_or_create(
                name=name, defaults={"branch": branch, "lat": lat, "lon": lon}
            )
            stores.append(store)
        for i, (image, name, category, variant) in enumerate(PRODUCTS):
            p, _ = Product.objects.get_or_create(
                sku=f"TE-{1001 + i}",
                defaults={
                    "name": name,
                    "category": category,
                    "variant": variant,
                    "image": f"/img/{image}.webp",
                },
            )
            for j, store in enumerate(stores):
                Stock.objects.get_or_create(
                    store=store,
                    product=p,
                    defaults={
                        "available": 0 if (i + j) % 5 == 0 else 2 + (i + j * 3) % 14
                    },
                )
        groups = [Group.objects.get_or_create(name=r)[0] for r in ROLES]
        email = "miguel@inventormiguel.com"
        if not User.objects.filter(username=email).exists():
            password = os.getenv("TE_ADMIN_PASSWORD")
            if not password:
                raise CommandError(
                    "Defina TE_ADMIN_PASSWORD para criar a conta inicial."
                )
            user = User.objects.create_superuser(
                email, email, password, first_name="Miguel"
            )
            user.groups.set(groups)
            Profile.objects.create(user=user, store=stores[0])
