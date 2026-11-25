from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ChangePasswordView, LoginView, LogoutView, MeView, UserViewSet

router = DefaultRouter()
router.register(r"users", UserViewSet, basename="user")

urlpatterns = [
    path("login/", LoginView.as_view(), name="account-login"),
    path("logout/", LogoutView.as_view(), name="account-logout"),
    path("me/", MeView.as_view(), name="account-me"),
    path("change-password/", ChangePasswordView.as_view(), name="account-change-password"),
    path("", include(router.urls)),
]
