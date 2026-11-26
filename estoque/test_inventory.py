from io import BytesIO
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from openpyxl import Workbook, load_workbook

from .inventory import HEADERS
from .models import Branch, Product, Stock, Store


class InventoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch.dict(os.environ, TE_ADMIN_PASSWORD="fixture"):
            call_command("seed_demo")
        cls.admin = User.objects.get(is_superuser=True)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_branch_product_store_and_manual_balance(self):
        self.client.post("/filiais/", {"action": "create", "name": "Sul"})
        b = Branch.objects.get(name="Sul")
        self.client.post(
            "/filiais/", {"action": "rename", "branch": b.pk, "name": "Sul 2"}
        )
        self.client.post(
            "/filiais/",
            {
                "action": "store",
                "branch": b.pk,
                "name": "Nova",
                "lat": "-21.5",
                "lon": "-44",
            },
        )
        self.client.post(
            "/produtos/",
            {"sku": "abc", "name": "Blusa", "category": "Vestuário", "variant": "G"},
        )
        p = Product.objects.get(sku="ABC")
        store = Store.objects.get(name="Nova", branch__name="Sul 2")
        self.assertEqual(Stock.objects.filter(product=p).count(), 6)
        self.assertEqual(Stock.objects.filter(store=store).count(), 13)
        stock = Stock.objects.get(product=p, store=store)
        path = f"/produtos/{p.pk}/estoque/"
        self.client.post(path, {"stock": stock.pk, "previous": 0, "available": 12})
        self.client.post(path, {"stock": stock.pk, "previous": 0, "available": 3})
        self.assertEqual(Stock.objects.get(pk=stock.pk).available, 12)
        self.assertContains(self.client.get("/visao/Vendedor/", {"q": "ABC"}), "Nova")
        self.assertContains(self.client.get(path), "Sul 2")
        duplicate = self.client.post(
            "/produtos/", {"sku": "abc", "name": "Outra", "category": "A"}, follow=True
        )
        self.assertContains(duplicate, "Já existe um produto")

    def upload(self, rows):
        book = Workbook()
        sheet = book.active
        sheet.append(HEADERS)
        for row in rows:
            sheet.append(row)
        out = BytesIO()
        book.save(out)
        return self.client.post(
            "/produtos/estoque/importar/",
            {"file": SimpleUploadedFile("estoque.xlsx", out.getvalue())},
        )

    def test_preview_confirm_and_stale_stock_rollback(self):
        book = load_workbook(
            BytesIO(self.client.get("/produtos/estoque/modelo/").content)
        )
        records = list(book.active.values)
        self.assertEqual(records[0], HEADERS)
        self.assertEqual(len(records), 61)
        rows = [[*r[:3], r[3] + 10] for r in records[1:3]]
        first = Stock.objects.get(store__name=rows[0][1], product__sku=rows[0][2])
        second = Stock.objects.get(store__name=rows[1][1], product__sku=rows[1][2])
        preview = self.upload(rows)
        self.assertContains(preview, "Confirmar importação")
        token = preview.context["token"]
        second.available += 1
        second.save()
        confirm = {"step": "confirm", "token": token}
        self.client.post("/produtos/estoque/importar/", confirm)
        first.refresh_from_db()
        self.assertEqual(first.available, records[1][3])
        confirm["token"] = self.upload(rows).context["token"]
        self.client.post("/produtos/estoque/importar/", confirm)
        self.client.post("/produtos/estoque/importar/", confirm)
        self.assertEqual(Stock.objects.get(pk=first.pk).available, rows[0][3])
        self.assertEqual(Stock.objects.get(pk=second.pk).available, rows[1][3])

    def test_invalid_import_and_permissions(self):
        row = ("Filial Paulista", "Paulista", "TE-1001", 20)
        for bad in [row, (*row[:3], -4), ("X", *row[1:]), (*row[:3], "=30")]:
            self.assertEqual(self.upload([row, bad]).status_code, 302)
            self.assertNotIn("stock_preview", self.client.session)
        self.client.force_login(User.objects.create_user("seller"))
        for url in [
            "/filiais/",
            "/produtos/",
            "/produtos/1/estoque/",
            "/produtos/estoque/modelo/",
        ]:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.upload([row]).status_code, 403)
