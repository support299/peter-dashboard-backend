from django.urls import path

from accounts import users_views, views
from accounts.authz import protect

urlpatterns = [
    path("login/", views.login_view),
    path("refresh/", views.refresh_view),
    path("logout/", views.logout_view),
    path("me/", protect(views.me_view)),
    path("change-password/", protect(views.change_password_view)),
    path("forgot-password/", views.forgot_password_view),
    path("reset-password/", views.reset_password_view),
    # Admins only (checked in the views): add people and choose what they can see.
    path("users/", protect(users_views.users_view)),
    path("users/<int:pk>/", protect(users_views.user_detail_view)),
]
