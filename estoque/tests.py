import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from io import BytesIO
from datetime import datetime, timezone
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

    def test_admin_edits_own_stores_and_audits_changes(self):
        primary = self.admin.profile.store
        extra = Store.objects.get(name="Moema")
        path = f"/usuarios/{self.admin.pk}/"
        data = {"name": "Miguel", "store": primary.pk, "additional_stores": [extra.pk]}
        self.client.post(path, data)
        self.client.post(path, data)
        change = self.admin.account_changes.get()
        self.assertEqual(change.actor, self.admin)
        self.assertEqual(len(change.before["lojas"]), 1)
        self.assertEqual(len(change.after["lojas"]), 2)
        self.assertContains(self.client.get(path), "Alterações de cadastro")
        self.client.post("/loja-em-atendimento/", {"store": extra.pk})
        self.assertEqual(self.client.get("/visao/Vendedor/").context["store"], extra)
        order_data = {
            "selling_store": extra.pk,
            "stock": self.stock.pk,
            "quantity": 1,
            "token": uuid.uuid4(),
        }
        self.client.post("/solicitar/", order_data)
        item = Transfer.objects.get()
        self.assertEqual(item.destination, extra)
        self.assertIn(item, self.client.get("/visao/Gerente/").context["orders"])
        self.client.post(path, {"store": primary.pk, "name": "Miguel"})
        self.assertNotIn(item, self.client.get("/visao/Gerente/").context["orders"])
        self.assertEqual(self.client.get("/visao/Vendedor/").context["store"], primary)
        self.assertEqual(self.client.post("/solicitar/", order_data).status_code, 404)

    def test_seller_association_is_managed_and_readable_by_admin(self):
        primary = self.admin.profile.store
        extra = Store.objects.get(name="Pinheiros")
        payload = {
            "name": "Joana",
            "email": "joana@example.com",
            "password": "test-secret",
            "roles": ["Vendedor"],
            "store": primary.pk,
            "additional_stores": [extra.pk],
        }
        self.client.post("/usuarios/", payload)
        seller = User.objects.get(username="joana@example.com")
        self.assertEqual(seller.account_changes.get().after["perfis"], ["Vendedor"])
        self.assertContains(self.client.get("/usuarios/"), "Auditar usuário")
        self.client.force_login(seller)
        path = f"/usuarios/{seller.pk}/"
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(path, payload).status_code, 403)
        self.client.post("/loja-em-atendimento/", {"store": extra.pk})
        self.assertEqual(self.client.get("/visao/Vendedor/").context["store"], extra)
        self.assertEqual(
            self.client.post(
                "/loja-em-atendimento/", {"store": self.stock.store_id}
            ).status_code,
            404,
        )

    def test_dispatch_card_identifies_actual_approver_and_local_times(self):
        item = self.order()
        manager = User.objects.create_user(
            "gerente@example.com", first_name="Ana", last_name="Lima"
        )
        Profile.objects.create(user=manager, store=item.destination)
        manager.groups.add(Group.objects.get(name="Gerente"))
        self.client.force_login(manager)
        self.step(item, "aprovar")
        Transfer.objects.filter(pk=item.pk).update(
            created=datetime(2026, 11, 27, 1, 20, tzinfo=timezone.utc)
        )
        item.events.filter(status="Aprovada").update(
            created=datetime(2026, 11, 27, 4, 10, tzinfo=timezone.utc)
        )
        self.client.force_login(self.admin)
        self.step(item, "expedir", invoice="123")
        html = self.client.get("/visao/Almoxarifado/").content.decode()
        summary = html.split('class="dispatch-times"', 1)[1].split("<details>", 1)[0]
        for value in (
            "26/11/2026 às 22:20",
            "27/11/2026 às 01:10",
            "Ana Lima",
            "Horário de Brasília",
        ):
            self.assertIn(value, summary)

    def test_phone_creation_update_validation_and_removal(self):
        store = self.admin.profile.store
        data = {
            "name": "Contato",
            "email": "contato@example.com",
            "roles": ["Vendedor"],
            "store": store.pk,
            "password": "test-secret",
            "phone": "(11) 99999-1234",
        }
        self.client.post("/usuarios/", data)
        user = User.objects.get(username=data["email"])
        path = f"/usuarios/{user.pk}/"
        self.assertEqual(user.profile.phone, "+5511999991234")
        self.assertContains(self.client.get(path), 'value="+5511999991234"')
        data["phone"] = "+351 912 345 678"
        self.client.post(path, data)
        log = user.account_changes.latest("pk")
        self.assertEqual(log.before["telefone"], "+5511999991234")
        self.assertEqual(log.after["telefone"], "+351912345678")
        self.client.post(path, {**data, "phone": "texto"})
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.phone, "+351912345678")
        self.client.post(path, {k: v for k, v in data.items() if k != "phone"})
        self.assertEqual(user.account_changes.count(), 2)
        self.client.post(path, {**data, "phone": ""})
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.phone, "")

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
