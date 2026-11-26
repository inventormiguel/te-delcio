import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from PIL import Image

from .models import Event, Profile, Receipt, Stock, Store, Transfer


class TransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch.dict(os.environ, TE_ADMIN_PASSWORD="test-fixture-secret"):
            call_command("seed_demo")
        cls.admin = User.objects.get(is_superuser=True)
        cls.stock = Stock.objects.get(store__name="Jardins", product__sku="TE-1001")

    def setUp(self):
        self.client.force_login(self.admin)

    def order(self):
        data = {"quantity": 1, "stock": self.stock.pk, "token": uuid.uuid4()}
        self.client.post("/solicitar/", data)
        self.client.post("/solicitar/", data)
        return Transfer.objects.get()

    def step(self, t, action, **data):
        return self.client.post(f"/pedidos/{t.pk}/{action}/", data)

    def test_lifecycle_and_inventory(self):
        t = self.order()
        original = self.stock.available
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.available, original - 1)
        self.assertEqual(
            self.client.get("/visao/Almoxarifado/").context["orders"].count(), 0
        )
        self.step(t, "entregar")
        self.step(t, "aprovar")
        self.step(t, "expedir")
        t.refresh_from_db()
        self.assertEqual(t.status, "Aprovada")
        self.step(t, "expedir", invoice="NF-123")
        self.step(t, "entregar")
        self.step(t, "receber")
        t.refresh_from_db()
        self.assertEqual(t.status, "Entregue")
        img = BytesIO()
        Image.new("RGB", (8, 8)).save(img, "JPEG")
        self.step(
            t,
            "receber",
            photo=SimpleUploadedFile(
                "foto.jpg", img.getvalue(), content_type="image/jpeg"
            ),
        )
        self.assertEqual(Event.objects.filter(transfer=t).count(), 5)
        self.assertTrue(Receipt.objects.filter(transfer=t).exists())
        self.assertEqual(
            Stock.objects.get(store=t.destination, product=t.product).available, 1
        )
        self.assertEqual(self.client.get(f"/foto/{t.pk}/Vendedor/").status_code, 200)

    def test_reject_returns_stock_once_and_overbooking_fails(self):
        t = self.order()
        self.step(t, "recusar")
        self.step(t, "recusar")
        self.assertEqual(
            Stock.objects.get(pk=self.stock.pk).available, self.stock.available
        )
        self.client.post(
            "/solicitar/",
            {"stock": self.stock.pk, "quantity": 1000, "token": uuid.uuid4()},
        )
        self.assertEqual(Transfer.objects.count(), 1)

    def test_role_and_store_boundaries(self):
        t = self.order()
        for role in ("Gerente", "Almoxarifado", "Transportadora", "Vendedor"):
            self.client.force_login(self.admin)
            email = role.lower() + "@example.com"
            self.client.post(
                "/usuarios/",
                {
                    "name": role,
                    "email": email,
                    "password": "sample-password",
                    "roles": [role],
                    "store": Store.objects.get(name="Moema").pk,
                },
            )
            self.client.force_login(User.objects.get(username=email))
            response = self.client.get("/")
            self.assertEqual(response.url, f"/visao/{role}/")
            self.assertEqual(self.client.get(response.url).status_code, 200)
            self.assertEqual(self.client.get("/usuarios/").status_code, 403)
            other = "Gerente" if role != "Gerente" else "Vendedor"
            self.assertEqual(self.client.get(f"/visao/{other}/").status_code, 403)
            self.assertIn(self.step(t, "aprovar").status_code, [403, 404])
        t.refresh_from_db()
        self.assertEqual(t.status, "Aguardando aprovação")

    def test_requests_go_to_managers_of_the_sellers_store(self):
        # The administrator manages Paulista. The seller and two managers work in Jardins.
        store = self.stock.store

        def member(name, role):
            user = User.objects.create_user(name, first_name=name)
            Profile.objects.create(user=user, store=store)
            user.groups.add(Group.objects.get(name=role))
            return user

        seller = member("Ana", "Vendedor")
        first = member("Bia", "Gerente")
        second = member("Caio", "Gerente")
        self.client.force_login(seller)
        source_stock = Stock.objects.get(
            store__name="Moema", product=self.stock.product
        )
        self.client.post(
            "/solicitar/",
            {
                "stock": source_stock.pk,
                "quantity": 1,
                "token": uuid.uuid4(),
                "requester": self.admin.pk,
                "destination": self.admin.profile.store_id,
            },
        )
        order = Transfer.objects.get()
        self.assertEqual((order.requester, order.destination), (seller, store))
        self.client.force_login(self.admin)
        self.assertFalse(self.client.get("/visao/Gerente/").context["orders"].exists())
        self.assertEqual(self.step(order, "aprovar").status_code, 404)
        for manager in (first, second):
            self.client.force_login(manager)
            queue = self.client.get("/visao/Gerente/")
            self.assertContains(queue, "Vendedor solicitante · Ana")
            self.assertContains(
                queue, "Unidade solicitante · Filial Jardins · Loja Jardins"
            )
        self.step(order, "aprovar")
        self.client.force_login(first)
        self.step(order, "recusar")
        order.refresh_from_db()
        self.assertEqual(order.status, "Aprovada")
        self.assertEqual(order.events.get(status="Aprovada").actor, second)

    def test_search_and_nearest(self):
        for query in ("TE-1001", "camisa xadrez"):
            response = self.client.get("/visao/Vendedor/", {"q": query})
            products = list(response.context["products"])
            self.assertEqual(len(products), 1)
            self.assertEqual(products[0].options[0].store.name, "Jardins")

    def test_simulated_dispatch_note(self):
        item = self.order()
        self.step(item, "aprovar")
        self.step(item, "expedir", simular="1")
        response = self.client.get(f"/nota/{item.pk}/Almoxarifado/")
        self.assertContains(response, f"SIM-TE-{item.pk:06d}")
        self.assertContains(response, "sem valor fiscal")


class ReservationConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_two_requests_for_last_unit(self):
        with patch.dict(os.environ, TE_ADMIN_PASSWORD="test-fixture-secret"):
            call_command("seed_demo")
        user = User.objects.get(is_superuser=True)
        stock = Stock.objects.get(store__name="Jardins", product__sku="TE-1001")
        stock.available = 1
        stock.save(update_fields=["available"])
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(user)
        barrier = Barrier(2)

        def submit(client):
            try:
                barrier.wait(timeout=10)
                return client.post(
                    "/solicitar/",
                    {"token": uuid.uuid4(), "stock": stock.pk, "quantity": 1},
                ).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(submit, clients)), [302, 302])
        stock.refresh_from_db()
        self.assertEqual(stock.available, 0)
        self.assertEqual(Transfer.objects.count(), 1)
        self.assertEqual(Event.objects.count(), 1)
