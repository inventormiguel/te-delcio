from functools import wraps
from io import BytesIO
import math
import secrets
from zipfile import BadZipFile, ZipFile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

from .models import Branch, Product, Stock, Store
from .views import common

HEADERS = ("Filial", "Loja", "Código do produto", "Saldo disponível")
MAX_ROWS = 5000


def admin_only(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def text_field(post, key, size, label):
    value = post.get(key, "").strip()
    if not value or len(value) > size:
        raise ValidationError(f"Informe {label} com até {size} caracteres.")
    return value


def lock_inventory_catalog():
    # A store and a product created concurrently must both see the other at stock initialization.
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(10482927)")


@admin_only
def branches(request):
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            with transaction.atomic():
                name = text_field(request.POST, "name", 90, "o nome")
                if action == "create":
                    Branch.objects.create(name=name)
                elif action == "rename":
                    branch = get_object_or_404(Branch, pk=request.POST.get("branch"))
                    branch.name = name
                    branch.save(update_fields=["name"])
                elif action == "store":
                    lock_inventory_catalog()
                    branch = get_object_or_404(Branch, pk=request.POST.get("branch"))
                    lat = float(request.POST.get("lat", "").replace(",", "."))
                    lon = float(request.POST.get("lon", "").replace(",", "."))
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError
                    store = Store.objects.create(
                        branch=branch, name=name, lat=lat, lon=lon
                    )
                    Stock.objects.bulk_create(
                        [
                            Stock(store=store, product_id=pk, available=0)
                            for pk in Product.objects.values_list("pk", flat=True)
                        ],
                        ignore_conflicts=True,
                    )
                else:
                    raise ValidationError("Ação de cadastro inválida.")
            messages.success(
                request, "Filial salva." if action != "store" else "Loja cadastrada."
            )
        except IntegrityError:
            messages.error(request, "Esse nome já está cadastrado.")
        except ValueError:
            messages.error(request, "Confira a latitude e a longitude da loja.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect("branches")
    return render(
        request,
        "branches.html",
        dict(
            common(request.user, "Filiais"),
            branches=Branch.objects.prefetch_related("store_set").order_by("name"),
        ),
    )


@admin_only
def products(request):
    if request.method == "POST":
        try:
            with transaction.atomic():
                lock_inventory_catalog()
                sku = text_field(request.POST, "sku", 24, "o código").upper()
                if Product.objects.filter(sku__iexact=sku).exists():
                    raise ValidationError("Já existe um produto com esse código.")
                variant = request.POST.get("variant", "").strip()
                if len(variant) > 50 or len(sku) > 24:
                    raise ValidationError(
                        "Confira o limite de caracteres do código e da variação."
                    )
                product = Product.objects.create(
                    sku=sku,
                    name=text_field(request.POST, "name", 120, "o nome"),
                    category=text_field(request.POST, "category", 40, "a categoria"),
                    variant=variant,
                    image="",
                )
                Stock.objects.bulk_create(
                    [
                        Stock(store_id=s, product=product, available=0)
                        for s in Store.objects.values_list("pk", flat=True)
                    ],
                    ignore_conflicts=True,
                )
            messages.success(
                request, "Produto cadastrado. Informe o saldo disponível nas lojas."
            )
            return redirect("product_stock", pk=product.pk)
        except IntegrityError:
            messages.error(request, "Já existe um produto com esse código.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect("products")
    q = request.GET.get("q", "").strip()
    items = Product.objects.filter(Q(sku__icontains=q) | Q(name__icontains=q)).order_by(
        "name", "sku"
    )
    return render(
        request,
        "products.html",
        dict(common(request.user, "Produtos"), items=items, q=q),
    )


def quantity(value):
    if type(value) is float and math.isfinite(value) and value.is_integer():
        value = int(value)
    if (
        not str(value).isascii()
        or not str(value).isdigit()
        or len(str(value)) > 10
        or int(value) > 2147483647
    ):
        raise ValueError
    return int(value)


@admin_only
def product_stock(request, pk):
    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        try:
            row = get_object_or_404(
                Stock, pk=request.POST.get("stock"), product=product
            )
            expected = quantity(request.POST.get("previous"))
            new = quantity(request.POST.get("available"))
            if Stock.objects.filter(pk=row.pk, available=expected).update(
                available=new
            ):
                messages.success(request, "Saldo disponível atualizado.")
            else:
                messages.error(
                    request,
                    "O saldo mudou durante a edição. Confira o valor atual antes de salvar.",
                )
        except ValueError:
            messages.error(request, "Informe uma quantidade inteira a partir de zero.")
        return redirect("product_stock", pk=pk)
    return render(
        request,
        "product_stock.html",
        dict(
            common(request.user, "Produtos"),
            product=product,
            stocks=product.stock_set.select_related("store__branch").order_by(
                "store__branch__name", "store__name"
            ),
        ),
    )


@admin_only
def stock_template(request):
    book = Workbook()
    sheet = book.active
    sheet.title = "Estoque"
    sheet.append(HEADERS)
    stocks = Stock.objects.select_related("store__branch", "product").order_by(
        "store__branch__name", "store__name", "product__sku"
    )
    for index, row in enumerate(stocks.iterator(), start=2):
        sheet.append(
            (row.store.branch.name, row.store.name, row.product.sku, row.available)
        )
        for col in (1, 2, 3):
            sheet.cell(row=index, column=col).data_type = "s"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4D86")
    for col, width in zip("ABCD", (28, 28, 25, 20)):
        sheet.column_dimensions[col].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    output = BytesIO()
    book.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="estoque-te.xlsx"'
    return response


def preview_stock(upload):
    if (
        not upload
        or not upload.name.lower().endswith(".xlsx")
        or upload.size > 3_000_000
    ):
        raise ValidationError("Envie um arquivo Excel (.xlsx) de até 3 MB.")
    try:
        with ZipFile(upload) as archive:
            if sum(f.file_size for f in archive.infolist()) > 32_000_000:
                raise ValidationError("A planilha excede o tamanho permitido.")
        upload.seek(0)
        book = load_workbook(upload, read_only=True, data_only=False, keep_links=False)
        try:
            sheet = book["Estoque"] if "Estoque" in book.sheetnames else book.active
            sheet.reset_dimensions()
            rows = sheet.values
            if tuple(next(rows, ())) != HEADERS:
                raise ValidationError(
                    "Use os quatro cabeçalhos do modelo, na mesma ordem."
                )
            balances = {
                (s.store.branch.name, s.store.name, s.product.sku): s
                for s in Stock.objects.select_related("store__branch", "product")
            }
            seen = set()
            changes = []
            for line, values in enumerate(rows, 2):
                if line > MAX_ROWS + 1:
                    raise ValidationError(f"Use até {MAX_ROWS} linhas por importação.")
                if not any(v is not None for v in values):
                    continue
                if len(values) != 4:
                    raise ValidationError(
                        f"Linha {line}: preencha as quatro colunas do modelo."
                    )
                key = tuple(str(v).strip() for v in values[:3])
                stock = balances.get(key)
                if stock is None:
                    raise ValidationError(
                        f"Linha {line}: confira a filial, a loja e o código do produto cadastrado."
                    )
                if stock.pk in seen:
                    raise ValidationError(
                        f"Linha {line}: o produto aparece duas vezes na mesma loja."
                    )
                seen.add(stock.pk)
                try:
                    new = quantity(values[3])
                except ValueError:
                    raise ValidationError(
                        f"Linha {line}: saldo deve ser um número inteiro a partir de zero."
                    )
                if new != stock.available:
                    changes.append(
                        {
                            "id": stock.pk,
                            "branch": key[0],
                            "store": key[1],
                            "sku": key[2],
                            "old": stock.available,
                            "new": new,
                        }
                    )
            if not seen:
                raise ValidationError("A planilha está vazia.")
            return changes
        finally:
            book.close()
    except ValidationError:
        raise
    except (
        BadZipFile,
        InvalidFileException,
        KeyError,
        ValueError,
        OSError,
        TypeError,
        SyntaxError,
        AttributeError,
        RuntimeError,
    ):
        raise ValidationError(
            "Não foi possível ler a planilha. Use uma cópia do modelo Excel."
        )


@admin_only
@require_POST
def import_stock(request):
    try:
        if request.POST.get("step") == "confirm":
            pending = request.session.get("stock_preview")
            if not pending or request.POST.get("token") != pending["token"]:
                raise ValidationError(
                    "Esta prévia expirou. Envie a planilha novamente."
                )
            changes = {row["id"]: row for row in pending["changes"]}
            with transaction.atomic():
                stocks = list(
                    Stock.objects.select_for_update()
                    .filter(pk__in=changes)
                    .order_by("pk")
                )
                if len(stocks) != len(changes) or any(
                    s.available != changes[s.pk]["old"] for s in stocks
                ):
                    raise ValidationError(
                        "Um saldo mudou após a prévia. Envie a planilha novamente para revisar os valores atuais."
                    )
                for stock in stocks:
                    stock.available = changes[stock.pk]["new"]
                Stock.objects.bulk_update(stocks, ["available"], batch_size=250)
            request.session.pop("stock_preview", None)
            messages.success(
                request, f"Importação concluída. Saldos atualizados: {len(stocks)}."
            )
        else:
            request.session.pop("stock_preview", None)
            changes = preview_stock(request.FILES.get("file"))
            if changes:
                token = secrets.token_urlsafe(20)
                request.session["stock_preview"] = {"token": token, "changes": changes}
                return render(
                    request,
                    "stock_preview.html",
                    dict(
                        common(request.user, "Produtos"), changes=changes, token=token
                    ),
                )
            messages.success(
                request, "Os saldos da planilha já correspondem ao estoque."
            )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("products")
