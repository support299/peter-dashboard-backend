from django.urls import path

from accounts import views
from accounts.authz import protect

urlpatterns = [
    path("login/", views.login_view),
    path("refresh/", views.refresh_view),
    path("logout/", views.logout_view),
    path("me/", protect(views.me_view)),
    path("change-password/", protect(views.change_password_view)),
    path("forgot-password/", views.forgot_password_view),
    path("reset-password/", views.reset_password_view),
]
