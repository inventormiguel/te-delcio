import base64
import json
import os
import re
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import ApprovalWhatsApp, WhatsAppConnection
from .views import common

PHONE = r"^\+[1-9][0-9]{6,14}$"
STATES = {"offline", "waiting", "starting", "pairing", "ready", "error"}
LABELS = {
    "claimed": "Em processamento",
    "accepted": "Aceita pelo OpenWA",
    "uncertain": "Conferir envio no WhatsApp",
}


@never_cache
@login_required
def integration(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    notices = list(
        ApprovalWhatsApp.objects.select_related(
            "transfer__requester__profile"
        ).order_by("-created")[:25]
    )
    for notice in notices:
        seller = notice.transfer.requester
        notice.label = LABELS.get(notice.status, "Aguardando envio")
        if notice.status == "pending" and not re.fullmatch(PHONE, seller.profile.phone):
            notice.label = "Aguardando telefone com código do país"
    return render(
        request,
        "whatsapp.html",
        {
            **common(request.user, ""),
            "whatsapp_page": True,
            "notices": notices,
        },
    )


@never_cache
@login_required
@require_POST
def pairing(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    now = timezone.now()
    with transaction.atomic():
        link, _ = WhatsAppConnection.objects.select_for_update().get_or_create(pk=1)
        link.pairing_until = now + timedelta(seconds=40)
        link.save(update_fields=["pairing_until"])
        fresh = link.heartbeat and now - link.heartbeat < timedelta(seconds=65)
        qr_fresh = fresh and now - link.heartbeat < timedelta(seconds=12)
        return JsonResponse(
            {
                "status": link.status if fresh else "offline",
                "qr": link.qr_image if qr_fresh and link.status == "pairing" else "",
            }
        )


def claim_notice(claim):
    with transaction.atomic():
        item = ApprovalWhatsApp.objects.filter(claim=claim, status="claimed").first()
        if item is None:
            if ApprovalWhatsApp.objects.filter(claim=claim).exists():
                return None
            item = (
                ApprovalWhatsApp.objects.select_for_update(
                    skip_locked=True, of=("self",)
                )
                .filter(
                    status="pending",
                    transfer__requester__is_active=True,
                    transfer__requester__profile__phone__regex=PHONE,
                )
                .order_by("pk")
                .first()
            )
            if item is None:
                return None
            item.claim = claim
            item.status = "claimed"
            item.save(update_fields=["claim", "status", "updated"])
        order = item.transfer
        seller = order.requester
        if not seller.is_active or not re.fullmatch(PHONE, seller.profile.phone):
            item.status, item.claim = "pending", None
            item.save(update_fields=["claim", "status", "updated"])
            return None
        return {
            "phone": seller.profile.phone,
            "text": (
                f"Seu pedido TE-{order.pk:04d} foi aprovado.\n"
                f"{order.quantity} un. de {order.product.name} ({order.product.sku}).\n"
                f"{order.source.name} → {order.destination.name}.\n"
                "Acompanhe no TE: https://te-delcio.vercel.app/visao/Vendedor/?aba=pedidos"
            ),
        }


@csrf_exempt
@never_cache
@require_POST
def bridge(request):
    token = os.environ.get("TE_WHATSAPP_BRIDGE_TOKEN")
    if not token or not secrets.compare_digest(
        request.headers.get("Authorization", ""), "Bearer " + token
    ):
        raise PermissionDenied
    try:
        data = json.loads(request.body)
        state = data["status"]
        if state not in STATES:
            raise ValueError
        claim = uuid.UUID(data["claim"]) if data.get("claim") else None
        ack = data.get("ack")
        if ack:
            ack_id = uuid.UUID(ack["claim"])
            if (
                ack["status"] not in {"accepted", "uncertain"}
                or len(ack["provider_id"]) > 255
                or (ack["status"] == "accepted" and not ack["provider_id"])
            ):
                raise ValueError
        qr = data.get("qr", "")
        if qr:
            if (
                state != "pairing"
                or not qr.startswith("data:image/png;base64,")
                or len(qr) > 100000
            ):
                raise ValueError
            if not base64.b64decode(qr.split(",", 1)[1], validate=True).startswith(
                b"\x89PNG\r\n\x1a\n"
            ):
                raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError):
        return HttpResponseBadRequest()
    now = timezone.now()
    with transaction.atomic():
        link, _ = WhatsAppConnection.objects.select_for_update().get_or_create(pk=1)
        wanted = bool(link.pairing_until and link.pairing_until > now)
        link.heartbeat, link.status, link.qr_image = now, state, qr if wanted else ""
        link.save(update_fields=["heartbeat", "status", "qr_image"])
        if ack:
            ApprovalWhatsApp.objects.filter(claim=ack_id, status="claimed").update(
                status=ack["status"], provider_id=ack["provider_id"], updated=now
            )
        job = claim_notice(claim) if state == "ready" and claim else None
    return JsonResponse({"pair": wanted, "job": job})
