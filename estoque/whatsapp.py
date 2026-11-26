import base64
import json
import os
import re
import secrets
import uuid
from datetime import timedelta

import phonenumbers

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import ApprovalWhatsApp, ManagerWhatsApp, WhatsAppConnection
from .notifications import manager_message, managers_for
from .views import START, common

PHONE = r"^\+[1-9][0-9]{6,14}$"
STATES = {"offline", "waiting", "starting", "pairing", "ready", "error"}
LABELS = {
    "cancelled": "Cancelado",
    "claimed": "Em processamento",
    "accepted": "Aceita pelo OpenWA",
    "uncertain": "Conferir envio no WhatsApp",
}


def whatsapp_number(phone):
    if re.fullmatch(PHONE, phone):
        return phone
    if not re.fullmatch(r"[1-9][0-9]{6,14}", phone):
        return None
    # Only add the plus. Never prepend a default country or change supplied digits.
    candidate = "+" + phone
    try:
        number = phonenumbers.parse(candidate, None)
    except phonenumbers.NumberParseException:
        return None
    if phonenumbers.is_valid_number(number) and (
        phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        == candidate
    ):
        return candidate
    return None


def first_addressable(notices, phone_field):
    phones = (
        notices.exclude(**{phone_field: ""})
        .order_by("pk")
        .values_list("pk", phone_field)
    )
    for pk, phone in phones.iterator(chunk_size=100):
        if whatsapp_number(phone):
            item = notices.filter(pk=pk).first()
            if item is not None:
                return item
    return None


@never_cache
@login_required
def integration(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    sellers = ApprovalWhatsApp.objects.select_related("transfer__requester__profile")
    managers = ManagerWhatsApp.objects.select_related(
        "email_notice__recipient__profile"
    )
    notices = []
    for queue in (sellers, managers):
        for notice in queue.order_by("-created")[:25]:
            if isinstance(notice, ManagerWhatsApp):
                notice.person = notice.email_notice.recipient
                notice.order_id = notice.email_notice.transfer_id
                notice.purpose = "Aprovação solicitada ao gerente"
            else:
                notice.person = notice.transfer.requester
                notice.order_id = notice.transfer_id
                notice.purpose = "Aprovação comunicada ao vendedor"
            notice.label = LABELS.get(notice.status, "Aguardando envio")
            if notice.status == "pending" and not whatsapp_number(
                notice.person.profile.phone
            ):
                notice.label = "Aguardando telefone com código do país"
            notices.append(notice)
    notices.sort(key=lambda n: n.created, reverse=True)
    return render(
        request,
        "whatsapp.html",
        {
            **common(request.user, ""),
            "whatsapp_page": True,
            "notices": notices[:25],
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
    # bridge() holds the singleton connection lock across claim and acknowledgement.
    for queue in (ManagerWhatsApp, ApprovalWhatsApp):
        item = queue.objects.filter(claim=claim).first()
        if item:
            if item.status != "claimed":
                return None
            break
    else:
        eligible_manager = managers_for(
            OuterRef("email_notice__transfer__destination_id")
        ).filter(pk=OuterRef("email_notice__recipient_id"))
        pending = ManagerWhatsApp.objects.filter(status="pending")
        pending.exclude(
            Exists(eligible_manager), email_notice__transfer__status=START
        ).update(status="cancelled", updated=timezone.now())
        item = first_addressable(
            pending.filter(email_notice__transfer__status=START),
            "email_notice__recipient__profile__phone",
        )
        if item is None:
            item = first_addressable(
                ApprovalWhatsApp.objects.filter(
                    status="pending", transfer__requester__is_active=True
                ),
                "transfer__requester__profile__phone",
            )
        if item is None:
            return None

    if isinstance(item, ManagerWhatsApp):
        notice = item.email_notice
        order = notice.transfer
        person = notice.recipient
        if (
            order.status != START
            or not managers_for(order.destination_id).filter(pk=person.pk).exists()
        ):
            item.status = "cancelled"
            item.save(update_fields=["status", "updated"])
            return None
        message = manager_message(order)
    else:
        order = item.transfer
        person = order.requester
        message = (
            f"Seu pedido TE-{order.pk:04d} foi aprovado.\n"
            f"{order.quantity} un. de {order.product.name} ({order.product.sku}).\n"
            f"{order.source.name} → {order.destination.name}.\n"
            "Acompanhe no TE: https://te-delcio.vercel.app/visao/Vendedor/?aba=pedidos"
        )
    phone = whatsapp_number(person.profile.phone)
    if not person.is_active or not phone:
        item.status, item.claim = "pending", None
        item.save(update_fields=["claim", "status", "updated"])
        return None
    if item.status == "pending":
        # A manager may decide the order while the bridge selects a job.
        changed = (
            type(item)
            .objects.filter(pk=item.pk, status="pending")
            .update(status="claimed", claim=claim, updated=timezone.now())
        )
        if not changed:
            return None
    return {"phone": phone, "text": message}


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
            for queue in (ManagerWhatsApp, ApprovalWhatsApp):
                queue.objects.filter(claim=ack_id, status="claimed").update(
                    status=ack["status"], provider_id=ack["provider_id"], updated=now
                )
        job = claim_notice(claim) if state == "ready" and claim else None
    return JsonResponse({"pair": wanted, "job": job})
