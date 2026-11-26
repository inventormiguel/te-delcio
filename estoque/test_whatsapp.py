import json
import os
import uuid
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from .models import ApprovalWhatsApp, Profile, Stock, Transfer


class ApprovalWhatsAppTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with patch.dict(os.environ, TE_ADMIN_PASSWORD="test-secret"):
            call_command("seed_demo")
        cls.admin = User.objects.get(is_superuser=True)
        cls.seller = User.objects.create_user("seller@example.com", first_name="Ana")
        Profile.objects.create(
            user=cls.seller, store=cls.admin.profile.store, phone="+351912345678"
        )
        stock = Stock.objects.get(store__name="Jardins", product__sku="TE-1001")
        cls.order = Transfer.objects.create(
            source=stock.store,
            destination=cls.admin.profile.store,
            requester=cls.seller,
            quantity=1,
            token=uuid.uuid4(),
            product=stock.product,
        )

    def approve(self):
        self.client.force_login(self.admin)
        return self.client.post(f"/pedidos/{self.order.pk}/aprovar/")

    def bridge(self, data):
        with patch.dict(os.environ, TE_WHATSAPP_BRIDGE_TOKEN="fixture"):
            return self.client.post(
                "/integracoes/whatsapp/servico/",
                json.dumps(data),
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer fixture",
            )

    def test_approval_sent_once_to_requester(self):
        self.approve()
        self.approve()
        self.assertEqual(ApprovalWhatsApp.objects.count(), 1)
        claim = str(uuid.uuid4())
        self.assertIsNone(
            self.bridge({"status": "waiting", "claim": claim}).json()["job"]
        )
        job = self.bridge({"status": "ready", "claim": claim}).json()["job"]
        self.assertEqual(job["phone"], self.seller.profile.phone)
        self.assertIn(f"TE-{self.order.pk:04d}", job["text"])
        self.assertEqual(
            self.bridge({"status": "ready", "claim": claim}).json()["job"], job
        )
        self.assertIsNone(
            self.bridge({"status": "ready", "claim": str(uuid.uuid4())}).json()["job"]
        )
        self.bridge(
            {
                "status": "ready",
                "ack": {"claim": claim, "status": "accepted", "provider_id": "id1"},
            }
        )
        self.assertIsNone(
            self.bridge({"status": "ready", "claim": claim}).json()["job"]
        )
        self.assertEqual(ApprovalWhatsApp.objects.get().status, "accepted")

    def test_local_or_missing_phone_waits_and_uncertain_send_is_not_repeated(self):
        self.approve()
        for phone in ["", "912345678"]:
            Profile.objects.filter(user=self.seller).update(phone=phone)
            self.assertIsNone(
                self.bridge({"status": "ready", "claim": str(uuid.uuid4())}).json()[
                    "job"
                ]
            )
        Profile.objects.filter(user=self.seller).update(phone="+14155550101")
        claim = str(uuid.uuid4())
        self.assertTrue(self.bridge({"status": "ready", "claim": claim}).json()["job"])
        self.bridge(
            {
                "status": "ready",
                "ack": {"claim": claim, "status": "uncertain", "provider_id": ""},
            }
        )
        self.assertIsNone(
            self.bridge({"status": "ready", "claim": str(uuid.uuid4())}).json()["job"]
        )

    def test_pairing_restricted_and_qr_expires(self):
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get("/integracoes/whatsapp/").status_code, 403)
        url = "/integracoes/whatsapp/pareamento/"
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(
            self.client.post("/integracoes/whatsapp/servico/").status_code, 403
        )
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url).json()["qr"], "")
        qr = "data:image/png;base64,iVBORw0KGgo="
        self.assertTrue(self.bridge({"status": "pairing", "qr": qr}).json()["pair"])
        response = self.client.post(url)
        self.assertEqual(response.json()["qr"], qr)
        self.assertIn("no-store", response["Cache-Control"])
        self.bridge({"status": "ready"})
        self.assertEqual(self.client.post(url).json()["qr"], "")
