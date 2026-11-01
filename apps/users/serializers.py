"""Serializers for users, roles and authentication."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.settings import api_settings

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.core.serializers import AuditFieldsMixin

from .models import Gender, IdentityDetail, UserStatus
from .services.sessions import active_session, issue_session_refresh

User = get_user_model()


class ReferenceSerializer(AuditFieldsMixin):
    """Base serializer for lookup tables built on ``ReferenceModel``."""

    class Meta:
        fields = (
            "id",
            "name",
            "description",
            "is_active",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = AuditFieldsMixin.AUDIT_FIELDS


class UserStatusSerializer(ReferenceSerializer):
    """Account lifecycle states."""

    class Meta(ReferenceSerializer.Meta):
        model = UserStatus


class GenderSerializer(ReferenceSerializer):
    """Gender options."""

    class Meta(ReferenceSerializer.Meta):
        model = Gender


class PermissionSerializer(serializers.ModelSerializer):
    """A single Django permission, presented by its full label."""

    label = serializers.SerializerMethodField()
    app_label = serializers.CharField(source="content_type.app_label", read_only=True)

    class Meta:
        model = Permission
        fields = ("id", "name", "codename", "app_label", "label")

    def get_label(self, obj) -> str:
        """The ``app_label.codename`` string used throughout the role matrix."""
        return f"{obj.content_type.app_label}.{obj.codename}"


@extend_schema_field(str)
class PermissionLabelField(serializers.RelatedField):
    """A permission addressed as ``app_label.codename``.

    A bare codename is not unique. ``view_logentry`` exists in both ``admin``
    and ``auditlog``, so resolving one would raise ``MultipleObjectsReturned``
    and turn a save into a 500 - and on the way out, two different permissions
    would serialise to the same string, leaving a client unable to tell which
    one a role actually holds.

    The app label is what disambiguates them, and ``app_label.codename`` is
    already the form Django's own ``user.has_perm`` takes, so the wire format
    matches the vocabulary the rest of the system uses.
    """

    default_error_messages = {
        "invalid": "Expected a permission as 'app_label.codename'.",
        "does_not_exist": "Permission '{label}' does not exist.",
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", Permission.objects.select_related("content_type"))
        super().__init__(**kwargs)

    def to_representation(self, value: Permission) -> str:
        """Render the permission as ``app_label.codename``."""
        return f"{value.content_type.app_label}.{value.codename}"

    def to_internal_value(self, data) -> Permission:
        """Resolve ``app_label.codename`` back to a permission."""
        if not isinstance(data, str) or data.count(".") != 1:
            self.fail("invalid")

        app_label, codename = data.split(".")
        try:
            return self.get_queryset().get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist:
            self.fail("does_not_exist", label=data)


def requester_rank(context: dict) -> int | None:
    """The requesting user's rank, worked out once per response.

    Cached in the serializer context, which a list's rows share, so a page of
    roles or users costs one rank lookup rather than one per row.
    """
    from apps.users.services.roles import user_rank

    if "requester_rank" not in context:
        request = context.get("request")
        user = getattr(request, "user", None)
        authenticated = user is not None and user.is_authenticated
        context["requester_rank"] = user_rank(user) if authenticated else None
    return context["requester_rank"]


class RoleSerializer(serializers.ModelSerializer):
    """A role, exposed as a group plus its permission labels."""

    # Declared by hand to drop the model's automatic UniqueValidator, which runs
    # before validate_name and would answer "already exists" for a hidden role.
    name = serializers.CharField(max_length=150)
    permissions = PermissionLabelField(many=True, required=False)
    is_protected = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()
    can_assign = serializers.SerializerMethodField()
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = (
            "id",
            "name",
            "permissions",
            "is_protected",
            "can_manage",
            "can_assign",
            "user_count",
        )

    def validate_name(self, value: str) -> str:
        """Refuse a name that ranks above the requester, then a duplicate.

        In that order, so trying to create "superadmin" is not answered with
        "already exists" - which would tell a manager or an admin that a role
        they are not shown is there.
        """
        from apps.users.services.roles import hidden_role_names

        request = self.context.get("request")
        if request is not None and value in hidden_role_names(request.user):
            raise serializers.ValidationError("This role name is reserved.")

        duplicates = Group.objects.filter(name=value)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError("A role with this name already exists.")
        return value

    def get_is_protected(self, obj) -> bool:
        """True when the API refuses to rename, delete or re-scope this role."""
        from apps.users.roles import PROTECTED_ROLES

        return obj.name in PROTECTED_ROLES

    def get_can_manage(self, obj) -> bool:
        """True when the requester may rename, re-permission or delete this role."""
        return not self.get_is_protected(obj) and self.get_can_assign(obj)

    def get_can_assign(self, obj) -> bool:
        """True when the requester may give this role to someone, or take it away."""
        from apps.users.services.roles import rank_allows, role_rank

        actor_rank = requester_rank(self.context)
        return actor_rank is not None and rank_allows(actor_rank, role_rank(obj.name))

    def get_user_count(self, obj) -> int:
        """How many users hold this role."""
        return obj.user_set.count()


class UserSerializer(AuditFieldsMixin):
    """Full user representation used by the ``/users/`` endpoints."""

    full_name = serializers.CharField(read_only=True)
    can_manage = serializers.SerializerMethodField()
    roles = serializers.SlugRelatedField(
        source="groups",
        many=True,
        slug_field="name",
        queryset=Group.objects.all(),
        required=False,
    )
    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, validators=[validate_password]
    )
    gender_detail = GenderSerializer(source="gender", read_only=True)
    user_status_detail = UserStatusSerializer(source="user_status", read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "first_name",
            "middle_name",
            "last_name",
            "full_name",
            "username",
            "email",
            "password",
            "phone_number",
            "emergency_contact_name",
            "emergency_contact_number",
            "date_of_birth",
            "gender",
            "gender_detail",
            "user_status",
            "user_status_detail",
            "bio",
            "avatar",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "timezone",
            "locale",
            "is_active",
            "is_staff",
            "last_login_at",
            "email_verified_at",
            "roles",
            "can_manage",
            "must_change_password",
            "must_complete_profile",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = (
            "last_login_at",
            "email_verified_at",
            "must_change_password",
            "must_complete_profile",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )

    def __init__(self, *args, **kwargs):
        """Resolve ``roles`` only among the roles the requester is shown.

        A hidden role named in a payload then fails as "does not exist", the
        same as a typo, rather than as a 403 that confirms it is real.
        """
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        roles = self.fields.get("roles")
        if request is not None and roles is not None and not roles.read_only:
            from apps.users.services.roles import hidden_role_names

            roles.child_relation.queryset = Group.objects.exclude(
                name__in=hidden_role_names(request.user)
            )

    def get_can_manage(self, obj) -> bool:
        """True when the requester may edit or delete this account."""
        from apps.users.services.roles import rank_allows, user_rank

        request = self.context.get("request")
        if request is not None and request.user.pk == obj.pk:
            return True
        actor_rank = requester_rank(self.context)
        return actor_rank is not None and rank_allows(actor_rank, user_rank(obj))

    def create(self, validated_data):
        """Create a user, hashing the password rather than storing it raw."""
        groups = validated_data.pop("groups", None)
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        if groups is not None:
            user.groups.set(groups)
        return user

    def update(self, instance, validated_data):
        """Update a user; a blank password means 'leave it unchanged'."""
        groups = validated_data.pop("groups", None)
        password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        if groups is not None:
            instance.groups.set(groups)
        return instance


class MeSerializer(UserSerializer):
    """The authenticated user's own record, including Django superuser status."""

    permissions = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = (*UserSerializer.Meta.fields, "is_superuser", "permissions")
        read_only_fields = (
            *UserSerializer.Meta.read_only_fields,
            "is_active",
            "is_staff",
            "is_superuser",
            "roles",
        )

    def get_permissions(self, obj) -> list[str]:
        """Flat list of permission labels, for driving UI visibility."""
        return sorted(obj.get_all_permissions())


class IdentityDetailSerializer(AuditFieldsMixin):
    """Identity document attached to a user."""

    class Meta:
        model = IdentityDetail
        fields = (
            "id",
            "user",
            "id_type",
            "id_number",
            "id_document",
            "issue_country",
            "issue_date",
            "expiry_date",
            "verified_at",
            *AuditFieldsMixin.AUDIT_FIELDS,
        )
        read_only_fields = AuditFieldsMixin.AUDIT_FIELDS


class UserCreateSerializer(serializers.Serializer):
    """Creating an account takes only an email and a role.

    Everything else is either the system's to set - country, status, a
    temporary password - or the new user's to fill in at first login.
    """

    email = serializers.EmailField()
    role = serializers.SlugRelatedField(slug_field="name", queryset=Group.objects.all())

    def __init__(self, *args, **kwargs):
        """Offer only the roles the requester is shown - see ``hidden_role_names``."""
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None:
            from apps.users.services.roles import hidden_role_names

            self.fields["role"].queryset = Group.objects.exclude(
                name__in=hidden_role_names(request.user)
            )

    def validate_email(self, value: str) -> str:
        """One account per email, deleted accounts included - the column is unique."""
        value = User.objects.normalize_email(value)
        if User.all_objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "An account with this email already exists."
            )
        return value


class FirstLoginPasswordSerializer(serializers.Serializer):
    """The password a new user chooses in place of their temporary one."""

    password = serializers.CharField(write_only=True)
    password_confirm = serializers.CharField(write_only=True)

    def validate(self, attrs):
        """Match the confirmation, then apply the project's password rules."""
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError(
                {"password_confirm": "The two password fields do not match."}
            )
        request = self.context.get("request")
        try:
            validate_password(attrs["password"], user=getattr(request, "user", None))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs


class FirstLoginProfileSerializer(serializers.ModelSerializer):
    """What a new user must tell the system about themselves before starting."""

    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    phone_number = serializers.CharField(max_length=50)
    gender = serializers.PrimaryKeyRelatedField(queryset=Gender.objects.all())

    class Meta:
        model = User
        fields = ("first_name", "middle_name", "last_name", "phone_number", "gender")


class ChangePasswordSerializer(serializers.Serializer):
    """Payload for an authenticated password change."""

    current_password = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)

    def validate(self, attrs):
        """Reject a mismatched confirmation."""
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError(
                {"password_confirm": "The two password fields do not match."}
            )
        return attrs


class LoginSerializer(TokenObtainPairSerializer):
    """Issues a token pair and returns the authenticated user alongside it.

    Returning the user in the login response saves the client an immediate
    follow-up call to ``/auth/me/`` just to render a name and a permission set.
    """

    @classmethod
    def get_token(cls, user):
        """Embed a couple of cheap claims for clients that decode the token."""
        token = issue_session_refresh(user)
        token["email"] = user.email
        token["full_name"] = user.full_name
        return token

    def validate(self, attrs):
        """Authenticate, then attach the serialised user to the response."""
        data = super().validate(attrs)
        data["user"] = MeSerializer(self.user, context=self.context).data
        return data


class SessionTokenRefreshSerializer(TokenRefreshSerializer):
    """Rotate tokens only while their login session remains active."""

    def validate(self, attrs):
        """Reject refresh tokens whose associated login session has expired."""
        refresh = self.token_class(attrs["refresh"])
        active_session(
            refresh.payload.get(api_settings.USER_ID_CLAIM), refresh.payload.get("sid")
        )
        return super().validate(attrs)


class LogoutSerializer(serializers.Serializer):
    """Payload naming the refresh token to blacklist."""

    refresh = serializers.CharField()
