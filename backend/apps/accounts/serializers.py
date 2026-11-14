from rest_framework import serializers
from .models import User


class UserSerializer(serializers.ModelSerializer):
    """
    Per DB Design §7 Rule #9: 'Do not expose password hashes or unrestricted storage keys through serializers.'
    """

    password = serializers.CharField(write_only=True, required=False, min_length=8)

    class Meta:
        model = User
        fields = [
            "id",
            "employee_id",
            "full_name",
            "email",
            "username",
            "password",
            "profile_image",
            "designation",
            "account_status",
            "is_active",
            "is_staff",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
