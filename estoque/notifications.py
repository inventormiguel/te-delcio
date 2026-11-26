import json
import logging
import os
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ApprovalEmail, ManagerWhatsApp

logger = logging.getLogger(__name__)


def managers_for(store_id):
    return (
        User.objects.filter(
            Q(is_superuser=True) | Q(groups__name="Gerente"),
            Q(profile__store_id=store_id) | Q(profile__additional_stores=store_id),
            is_active=True,
        )
        .exclude(email="")
        .distinct()
    )


def queue_manager_notices(order):
    now = timezone.now()
    ApprovalEmail.objects.bulk_create(
        [
            ApprovalEmail(transfer=order, recipient_id=pk, next_attempt=now)
            for pk in managers_for(order.destination_id).values_list("pk", flat=True)
        ],
        ignore_conflicts=True,
    )

    ManagerWhatsApp.objects.bulk_create(
        [
            ManagerWhatsApp(email_notice_id=pk)
            for pk in ApprovalEmail.objects.filter(transfer=order).values_list(
                "pk", flat=True
            )
        ],
        ignore_conflicts=True,
    )


def manager_message(order):
    number = f"TE-{order.pk:04d}"
    shop = order.destination
    return (
        f"O pedido {number} aguarda aprovação da gerência da Loja {shop.name} ({shop.branch.name}).\n\n"
        f"Solicitante: {order.requester.get_full_name() or order.requester.username}\n"
        f"Produto: {order.product.name} · {order.product.sku} · {order.product.variant}\n"
        f"Quantidade: {order.quantity}\n"
        f"Transferência: {order.source.name} → {shop.name}\n\n"
        "Entre no TE para revisar o pedido:\nhttps://te-delcio.vercel.app/visao/Gerente/"
    )


def send_approval_email(notice):
    order = notice.transfer
    payload = {
        "from": os.environ["TE_EMAIL_FROM"],
        "to": [notice.recipient.email],
        "subject": f"TE-{order.pk:04d} · Aprovação de transferência · Loja {order.destination.name}",
        "text": manager_message(order),
    }
    req = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["TE_RESEND_KEY"],
            "Content-Type": "application/json",
            "User-Agent": "TE/1.0",
            "Idempotency-Key": f"te-approval/{order.token}/{notice.recipient_id}",
        },
    )
    with urlopen(req, timeout=6) as response:
        return json.load(response)["id"]


def dispatch_pending(transfer_id=None, limit=5):
    if not os.environ.get("TE_EMAIL_FROM") or not os.environ.get("TE_RESEND_KEY"):
        logger.warning("Envio de e-mails do TE aguarda configuração de remetente.")
        return 0
    sent = 0
    for _ in range(limit):
        with transaction.atomic():
            pending = ApprovalEmail.objects.select_for_update(
                skip_locked=True, of=("self",)
            ).filter(
                accepted_at=None, cancelled_at=None, next_attempt__lte=timezone.now()
            )
            if transfer_id is not None:
                pending = pending.filter(transfer_id=transfer_id)
            notice = (
                pending.select_related(
                    "transfer__destination__branch",
                    "transfer__requester",
                    "transfer__product",
                    "transfer__source",
                    "recipient",
                )
                .order_by("next_attempt", "pk")
                .first()
            )
            if notice is None:
                break
            order = notice.transfer
            if (
                order.status != "Aguardando aprovação"
                or not managers_for(order.destination_id)
                .filter(pk=notice.recipient_id)
                .exists()
            ):
                notice.cancelled_at = timezone.now()
                notice.save(update_fields=["cancelled_at"])
                continue
            notice.attempts += 1
            try:
                notice.provider_id = send_approval_email(notice)
            except (OSError, ValueError, KeyError) as exc:
                notice.last_error = (
                    f"HTTP {exc.code}"
                    if isinstance(exc, HTTPError)
                    else type(exc).__name__
                )
                notice.next_attempt = timezone.now() + timedelta(
                    minutes=min(60, 2 ** min(notice.attempts, 6))
                )
                logger.warning(
                    "Aviso %s pendente após tentativa %s (%s)",
                    notice.pk,
                    notice.attempts,
                    notice.last_error,
                )
            else:
                notice.last_error = ""
                notice.accepted_at = timezone.now()
                sent += 1
            notice.save()
    return sent
