import os
import secrets
import math
import re
import uuid
from io import BytesIO

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST
from PIL import Image, ImageOps, UnidentifiedImageError

from .notifications import dispatch_pending, queue_approval_emails
from .models import (
    AccountChange,
    Event,
    Product,
    Profile,
    Receipt,
    Stock,
    Store,
    Transfer,
)

ROLES = ("Vendedor", "Gerente", "Almoxarifado", "Transportadora")
START = "Aguardando aprovação"
APPROVED = "Aprovada"
SHIPPED = "Em transporte"
DELIVERED = "Entregue"
DONE = "Recebida"
REJECTED = "Recusada"
STEPS = {
    "aprovar": ("Gerente", START, APPROVED),
    "recusar": ("Gerente", START, REJECTED),
    "expedir": ("Almoxarifado", APPROVED, SHIPPED),
    "entregar": ("Transportadora", SHIPPED, DELIVERED),
    "receber": ("Vendedor", DELIVERED, DONE),
}


def roles(user):
    granted = set(user.groups.values_list("name", flat=True))
    return [r for r in ROLES if user.is_superuser or r in granted]


def access(user, role):
    if role not in roles(user):
        raise PermissionDenied


def common(user, role):
    return {"roles": roles(user), "role": role, "store": user.profile.store}


def enter(request):
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("email", "").strip().lower(),
            password=request.POST.get("password", ""),
        )
        if user:
            login(request, user)
            return redirect("home")
        messages.error(request, "Confira o e-mail e a senha.")
    return render(request, "login.html")


@require_POST
def leave(request):
    logout(request)
    return redirect("enter")


@login_required
def home(request):
    available = roles(request.user)
    if not available:
        raise PermissionDenied
    return redirect("panel", role=available[0])


def distance(a, b):
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(math.radians(a.lon - b.lon) / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(h))


def visible(user, role):
    orders = Transfer.objects.select_related(
        "requester", "source", "destination__branch", "product"
    )
    stores = user.profile.linked_stores()
    if role == "Vendedor":
        return orders.filter(
            Q(requester=user)
            | (Q(source__in=stores) & ~Q(status__in=[START, REJECTED]))
        )
    if role == "Gerente":
        return orders.filter(destination__in=stores)
    if role == "Almoxarifado":
        orders = orders.exclude(status__in=[START, REJECTED])
        return orders if user.is_superuser else orders.filter(source__in=stores)
    return orders.filter(status__in=[SHIPPED, DELIVERED, DONE])


@login_required
def panel(request, role):
    access(request.user, role)
    ctx = common(request.user, role)
    if role == "Vendedor":
        linked = list(request.user.profile.linked_stores())
        selected_id = request.session.get("selling_store")
        ctx["store"] = next((s for s in linked if s.pk == selected_id), ctx["store"])
        ctx["linked_stores"] = linked
    orders = visible(request.user, role).order_by("-id")
    queue = request.GET.get("aba") == "pedidos" or role != "Vendedor"
    if queue:
        events = Event.objects.select_related("actor").order_by("created", "pk")
        orders = orders.prefetch_related(Prefetch("events", queryset=events))
        if role == "Almoxarifado":
            for item in orders:
                item.approval_event = next(
                    (event for event in item.events.all() if event.status == APPROVED),
                    None,
                )
        ctx.update(orders=orders, queue=True)
        return render(request, "queue.html", ctx)
    query = request.GET.get("q", "").strip()
    category = request.GET.get("categoria", "")
    products = Product.objects.filter(
        Q(name__icontains=query) | Q(sku__icontains=query)
    ).order_by("sku")
    if category:
        products = products.filter(category=category)
    balances = {}
    for row in (
        Stock.objects.exclude(store=ctx["store"])
        .filter(product__in=products, available__gt=0)
        .select_related("store")
    ):
        row.km = distance(ctx["store"], row.store)
        balances.setdefault(row.product_id, []).append(row)
    for product in products:
        product.options = sorted(
            balances.get(product.id, []), key=lambda row: (row.km, row.store_id)
        )
        product.token = uuid.uuid4()
    ctx.update(
        products=products,
        query=query,
        category=category,
        categories=Product.objects.values_list("category", flat=True)
        .distinct()
        .order_by("category"),
        stores=Store.objects.count(),
        total=Product.objects.count(),
        pending=orders.filter(status=START).count(),
    )
    return render(request, "catalog.html", ctx)


@login_required
@require_POST
def order(request):
    access(request.user, "Vendedor")
    try:
        qty = int(request.POST.get("quantity", ""))
        token = uuid.UUID(request.POST.get("token", ""))
        stock_id = int(request.POST.get("stock", ""))
        if qty < 1 or qty > 100000:
            raise ValueError
    except ValueError:
        messages.error(request, "Revise a loja e a quantidade.")
        return redirect("home")
    with transaction.atomic():
        stock = get_object_or_404(Stock, pk=stock_id)
        # Use the store displayed on the submitted form, even across browser tabs.
        dest_id = request.POST.get("selling_store", request.user.profile.store_id)
        dest = assigned_store(request.user, dest_id)
        if stock.store_id == dest.id:
            raise PermissionDenied
        transfer, created = Transfer.objects.get_or_create(
            token=token,
            defaults={
                "requester": request.user,
                "source_id": stock.store_id,
                "destination": dest,
                "product_id": stock.product_id,
                "quantity": qty,
            },
        )
        if transfer.requester_id != request.user.id:
            raise PermissionDenied
        if created:
            changed = Stock.objects.filter(pk=stock_id, available__gte=qty).update(
                available=F("available") - qty
            )
            if not changed:
                transaction.set_rollback(True)
                messages.error(
                    request,
                    "O saldo disponível mudou. Confira a quantidade na unidade.",
                )
                return redirect("home")
            Event.objects.create(transfer=transfer, actor=request.user, status=START)
            queue_approval_emails(transfer)
            transaction.on_commit(lambda: dispatch_pending(transfer.pk), robust=True)
    messages.success(
        request,
        f"Pedido TE-{transfer.id:04d} encaminhado à gerência da Loja {transfer.destination.name}.",
    )
    return redirect("/visao/Vendedor/?aba=pedidos")


def receipt_image(upload):
    if not upload or upload.size > 4_000_000:
        raise ValueError
    with Image.open(upload) as image:
        if (
            image.format not in ("JPEG", "PNG", "WEBP")
            or image.width * image.height > 20000000
        ):
            raise ValueError
        image = ImageOps.exif_transpose(image)
        image.thumbnail((1600, 1600))
        out = BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=83)
        return out.getvalue()


@login_required
@require_POST
@transaction.atomic
def advance(request, pk, action):
    if action not in STEPS:
        raise PermissionDenied
    role, old, new = STEPS[action]
    access(request.user, role)
    item = get_object_or_404(visible(request.user, role), pk=pk)
    if action == "receber" and item.requester_id != request.user.id:
        raise PermissionDenied
    fields = {"status": new}
    image = None
    if action == "expedir":
        invoice = request.POST.get("invoice", "").strip()
        if request.POST.get("simular") == "1":
            invoice = f"SIM-TE-{item.pk:06d}"
        if not invoice or len(invoice) > 100:
            messages.error(request, "Informe o número da nota para expedir.")
            return redirect("panel", role=role)
        fields["invoice"] = invoice
    if action == "receber":
        try:
            image = receipt_image(request.FILES.get("photo"))
        except (
            ValueError,
            OSError,
            Image.DecompressionBombError,
            UnidentifiedImageError,
        ):
            messages.error(request, "Envie uma foto JPG, PNG ou WebP de até 4 MB.")
            return redirect("/visao/Vendedor/?aba=pedidos")
    if Transfer.objects.filter(pk=pk, status=old).update(**fields):
        if new in (DONE, REJECTED):
            store_id = item.destination_id if new == DONE else item.source_id
            Stock.objects.filter(store_id=store_id, product_id=item.product_id).update(
                available=F("available") + item.quantity
            )
        if image:
            Receipt.objects.create(transfer=item, image=image)
        Event.objects.create(transfer=item, actor=request.user, status=new)
        messages.success(request, f"TE-{pk:04d} · {new}")
    else:
        messages.error(request, "O pedido já mudou de etapa. Confira o histórico.")
    return redirect(f"/visao/{role}/?aba=pedidos")


@login_required
def photo(request, pk, role):
    access(request.user, role)
    item = get_object_or_404(visible(request.user, role), pk=pk)
    response = HttpResponse(
        get_object_or_404(Receipt, transfer=item).image, content_type="image/jpeg"
    )
    response["Cache-Control"] = "private, no-store"
    return response


def contact_phone(raw):
    raw = raw.strip()
    if not raw:
        return ""
    number = re.sub(r"[\s().-]", "", raw)
    number = "".join(str(int(c)) if c.isdecimal() else c for c in number)
    if number.startswith("00"):
        number = "+" + number[2:]
    if not re.fullmatch(r"(?:\+[1-9]\d{1,14}|\d{1,15})", number):
        raise ValidationError(
            "Informe o número em formato local ou com + e o código do país."
        )
    return number


@login_required
def users(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        name = request.POST.get("name", "").strip()
        password = request.POST.get("password", "")
        selected = request.POST.getlist("roles")
        try:
            validate_email(email)
            phone = contact_phone(request.POST.get("phone", ""))
            if (
                len(email) > 150
                or not name
                or len(name) > 150
                or len(password) < 8
                or not selected
                or not set(selected) <= set(ROLES)
            ):
                raise ValueError
            try:
                store = Store.objects.get(pk=request.POST.get("store"))
            except (ValueError, Store.DoesNotExist):
                messages.error(request, "Selecione a filial e a loja do usuário.")
                return redirect("users")
            with transaction.atomic():
                user = User.objects.create_user(email, email, password, first_name=name)
                user.groups.set(Group.objects.filter(name__in=selected))
                profile = Profile.objects.create(user=user, store=store, phone=phone)
                profile.additional_stores.set(additional_ids(request, store))
                AccountChange.objects.create(
                    subject=user, actor=request.user, after=account_state(user)
                )
            messages.success(request, "Usuário criado.")
            return redirect("users")
        except IntegrityError:
            messages.error(request, "Esse e-mail já possui uma conta.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        except ValueError:
            messages.error(
                request,
                "Informe nome, e-mail, senha de pelo menos 8 caracteres e um perfil.",
            )
    return render(
        request,
        "users.html",
        dict(
            common(request.user, ""),
            stores_list=Store.objects.select_related("branch").order_by(
                "branch__name", "name"
            ),
            accounts=User.objects.select_related(
                "profile__store__branch"
            ).prefetch_related("groups", "profile__additional_stores__branch"),
            admin_page=True,
        ),
    )


@login_required
def dispatch_note(request, pk, role):
    access(request.user, role)
    item = get_object_or_404(visible(request.user, role).exclude(invoice=""), pk=pk)
    event = get_object_or_404(Event, transfer=item, status=SHIPPED)
    return render(request, "note.html", {"item": item, "event": event})


@login_required
@require_POST
def selling_store(request):
    access(request.user, "Vendedor")
    selected = assigned_store(request.user, request.POST.get("store"))
    request.session["selling_store"] = selected.pk
    return redirect("panel", role="Vendedor")


def assigned_store(user, value):
    try:
        store_id = int(value)
    except (ValueError, TypeError):
        raise PermissionDenied
    return get_object_or_404(user.profile.linked_stores(), pk=store_id)


def additional_ids(request, primary):
    ids = {int(value) for value in request.POST.getlist("additional_stores")} - {
        primary.pk
    }
    if Store.objects.filter(pk__in=ids).count() != len(ids):
        raise ValueError
    return ids


def account_state(user):
    profile = user.profile
    return {
        "nome": user.first_name,
        "telefone": profile.phone,
        "principal": f"{profile.store.branch.name} · Loja {profile.store.name}",
        "lojas": [f"{s.branch.name} · Loja {s.name}" for s in profile.linked_stores()],
        "perfis": roles(user),
    }


@login_required
def account(request, pk):
    if not request.user.is_superuser:
        raise PermissionDenied
    target = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        try:
            with transaction.atomic():
                target = User.objects.select_for_update().get(pk=pk)
                before = account_state(target)
                phone = contact_phone(request.POST.get("phone", target.profile.phone))
                name = request.POST.get("name", "").strip()
                primary = Store.objects.get(pk=request.POST.get("store"))
                extras = additional_ids(request, primary)
                selected = set(request.POST.getlist("roles"))
                if (
                    not name
                    or len(name) > 150
                    or (
                        not target.is_superuser
                        and (not selected or not selected <= set(ROLES))
                    )
                ):
                    raise ValueError
                target.first_name = name
                target.save(update_fields=["first_name"])
                target.profile.store = primary
                target.profile.phone = phone
                target.profile.save(update_fields=["store", "phone"])
                target.profile.additional_stores.set(extras)
                if not target.is_superuser:
                    target.groups.set(Group.objects.filter(name__in=selected))
                after = account_state(target)
                if after != before:
                    AccountChange.objects.create(
                        actor=request.user, subject=target, before=before, after=after
                    )
            messages.success(request, "Cadastro salvo.")
            return redirect("account", pk=pk)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        except (ValueError, Store.DoesNotExist):
            messages.error(
                request,
                "Confira o nome, a loja principal, as lojas adicionais e os perfis.",
            )
    return render(
        request,
        "account.html",
        {
            **common(request.user, ""),
            "admin_page": True,
            "account": target,
            "state": account_state(target),
            "stores_list": Store.objects.select_related("branch").order_by(
                "branch__name", "name"
            ),
            "extras": list(
                target.profile.additional_stores.values_list("pk", flat=True)
            ),
            "changes": target.account_changes.select_related("actor").order_by(
                "-created", "-pk"
            ),
            "activity": Event.objects.filter(actor=target)
            .select_related("transfer__product", "transfer__destination")
            .order_by("-created", "-pk"),
        },
    )


@require_GET
def retry_approval_emails(request):
    key = os.environ.get("CRON_SECRET")
    if not key or not secrets.compare_digest(
        request.headers.get("Authorization", ""), f"Bearer {key}"
    ):
        raise PermissionDenied
    response = JsonResponse({"accepted": dispatch_pending()})
    response["Cache-Control"] = "no-store"
    return response
