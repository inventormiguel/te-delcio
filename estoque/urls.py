from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("entrar/", views.enter, name="enter"),
    path("sair/", views.leave, name="leave"),
    path("visao/<str:role>/", views.panel, name="panel"),
    path("solicitar/", views.order, name="order"),
    path("pedidos/<int:pk>/<str:action>/", views.advance, name="advance"),
    path("nota/<int:pk>/<str:role>/", views.dispatch_note, name="note"),
    path("foto/<int:pk>/<str:role>/", views.photo, name="photo"),
    path("usuarios/<int:pk>/", views.account, name="account"),
    path("loja-em-atendimento/", views.selling_store, name="selling_store"),
    path("tarefas/avisos/", views.retry_approval_emails),
    path("usuarios/", views.users, name="users"),
]
