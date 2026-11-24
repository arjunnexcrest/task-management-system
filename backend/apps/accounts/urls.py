from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    ActivateAccountView,
    ChangePasswordView,
    ForgotPasswordView,
    LoginView,
    LogoutView,
    MeView,
    RefreshView,
    RegisterEmployeeView,
    ResetPasswordView,
    UserViewSet,
)

router = DefaultRouter()
router.register(r"users", UserViewSet, basename="user")

user_only_router = DefaultRouter()
user_only_router.register(r"", UserViewSet, basename="users")

urlpatterns = [
    path("login/", LoginView.as_view(), name="auth-login"),
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("change-password/", ChangePasswordView.as_view(), name="auth-change-password"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("reset-password/", ResetPasswordView.as_view(), name="auth-reset-password"),
    path("activate/", ActivateAccountView.as_view(), name="auth-activate"),
    path("register/", RegisterEmployeeView.as_view(), name="auth-register"),
    path("", include(router.urls)),
]
